"""PostgreSQL 后端：多副本共享状态（小/中流量首选，零新依赖）。

原子性实现
----------
`mutate` 在**单个数据库事务**内：`SELECT ... FOR UPDATE` 锁住该行（无则
INSERT），应用 fn，再 UPDATE。行锁保证并发调用串行化——与内存后端的全局锁
语义等价，但跨进程/跨副本生效。

为什么用 `SELECT FOR UPDATE` 而非乐观版本号：状态 key 的竞争是短时的（同设备
的限流/同部件的事件），行锁临界区极短、不会长事务；乐观重试在热点 key 上会
空转。选行锁更直接。

冲突重试：fn 在 `FOR UPDATE` 锁内调用一次即可——锁已把并发挡在外面，无需
像 CAS 那样重跑。但 INSERT（首次创建）时的并发仍可能撞唯一键，故用
`ON CONFLICT DO NOTHING` 后重取。

超时（失败关闭）
----------------
引擎参数复用 `storage.db.engine_kwargs`：`connect_timeout` / `pool_timeout` /
`statement_timeout` 都有上限（默认 5s / 5s / 15s）。限流检查在本后端是热路径，
数据库慢或锁等待失控时**必须快速报错**，而不是把调用线程静默钉住——见
`docs/reliability.md` 的审计表。
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import text

from .base import StateStore


class PostgresStateStore(StateStore):
    def __init__(self, url: str) -> None:
        from sqlalchemy import create_engine

        from ..storage.db import engine_kwargs
        # 引擎参数与业务库**共用同一实现**（storage.db.engine_kwargs）：建连/拿连接/
        # 语句三段等待都有上限。这里的状态读写夹在 `SELECT ... FOR UPDATE` 行锁里，
        # 一旦数据库慢或锁等待失控，无上限的等待会直接钉住调用方（限流检查在热路径上），
        # 故必须和业务库一样"拿不准就快速失败"。
        self._engine = create_engine(url, **engine_kwargs(url))

    def mutate(self, key: str, fn: Callable[[Any], tuple[Any, Any]]) -> Any:
        now = time.time()
        with self._engine.begin() as conn:
            # 尝试锁行；首次不存在则插入占位再锁（ON CONFLICT 处理并发建行）。
            row = conn.execute(
                text("SELECT value FROM state_kv WHERE key = :k FOR UPDATE"),
                {"k": key},
            ).fetchone()
            if row is None:
                conn.execute(
                    text("INSERT INTO state_kv (key, value, updated_at) "
                         "VALUES (:k, '{}', :t) ON CONFLICT (key) DO NOTHING"),
                    {"k": key, "t": now},
                )
                row = conn.execute(
                    text("SELECT value FROM state_kv WHERE key = :k FOR UPDATE"),
                    {"k": key},
                ).fetchone()
            cur = row[0] if row is not None else None
            # JSON 列在 PG 里是 dict；空占位 {} 视作 None（"不存在"），
            # 让 fn 用同一套逻辑处理"首次调用"。
            if cur == {}:
                cur = None
            new_value, result = fn(cur)
            conn.execute(
                text("UPDATE state_kv SET value = CAST(:v AS JSON), updated_at = :t "
                     "WHERE key = :k"),
                {"k": key, "v": _dump(new_value), "t": now},
            )
            return result

    def get(self, key: str) -> Any:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT value FROM state_kv WHERE key = :k"), {"k": key},
            ).fetchone()
        cur = row[0] if row is not None else None
        return None if cur == {} else cur

    def keys(self, prefix: str = "") -> list[str]:
        with self._engine.connect() as conn:
            if prefix:
                rows = conn.execute(
                    text("SELECT key FROM state_kv WHERE key LIKE :p ESCAPE '\\'"),
                    {"p": _like_prefix(prefix)},
                ).fetchall()
            else:
                rows = conn.execute(text("SELECT key FROM state_kv")).fetchall()
        return [r[0] for r in rows]

    def delete(self, key: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(text("DELETE FROM state_kv WHERE key = :k"), {"k": key})

    def close(self) -> None:
        self._engine.dispose()


def _dump(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _like_prefix(prefix: str) -> str:
    """把 prefix 转成 LIKE 模式，转义 `%`/`_`/`\\`（否则前缀里的这些字符会被当通配符）。"""
    esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return esc + "%"
