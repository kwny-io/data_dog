"""PostgreSQL 状态后端的**离线**单测：不连真库，只钉住它自己那几件事。

为什么这样测
-----------
`pg.py` 的 SQL 由 PG 执行，但"该发什么 SQL、参数怎么构造"是**本模块的逻辑**：
键/前缀的 LIKE 谓词转义、JSON 序列化口径、空占位 `{}` 归一到"不存在"、
首次创建走 `ON CONFLICT DO NOTHING`、以及**一次 mutate 只在一个事务里**
（原子性的前提）。把 `create_engine` 换成记录 SQL 的假引擎，这些都能离线验。

为什么不用 SQLite 顶替：本模块写的是 PG 方言（`FOR UPDATE`、`CAST(... AS JSON)`），
SQLite 解析不了，跑起来只能证明"方言写错了"，证明不了业务语义。
真库上才能验的部分（`ESCAPE '\\'` PG 是否认、JSON 列往返）用
`@pytest.mark.postgres` 的用例补，默认跳过（需 `DD_TEST_PG_URL`）。
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from typing import Any

import pytest

from data_dog.state.pg import PostgresStateStore, _dump, _like_prefix

_PG_URL = os.environ.get("DD_TEST_PG_URL", "").strip()

# ---------------------------------------------------------------- 假引擎（记录 SQL）

class _FakeResult:
    """模拟 CursorResult：只实现本模块用到的 fetchone / fetchall。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = list(rows)

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return list(self._rows)


class _FakeConn:
    """假连接：记录每次 (SQL, 参数)，按脚本队列返回行。

    脚本是**逐次 execute** 的行列表队列：第 n 次 execute 取队列第 n 项，
    取完（或不足）视为空结果集。这样才能模拟"先查不到 → INSERT → 再查"的时序。
    """

    def __init__(self, queue: list[list[Any]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._queue = queue

    def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        self.calls.append((" ".join(str(stmt).split()), dict(params or {})))
        rows = self._queue.pop(0) if self._queue else []
        return _FakeResult(rows)


class _FakeEngine:
    """假引擎：`begin()/connect()` 各自给一个新假连接，并记录连接次数。

    为什么记录连接数：一次 mutate 若用了两条连接，行锁与应用 fn 就不在同一事务里，
    "原子性"的承诺即失效——这是本模块最贵的一条不变量，值得钉死。
    """

    def __init__(self, *scripts: list[Any]) -> None:
        self.queue: list[list[Any]] = [list(s) for s in scripts]
        self.conns: list[_FakeConn] = []
        self.disposed = 0

    @contextmanager
    def _ctx(self) -> Any:
        conn = _FakeConn(self.queue)
        self.conns.append(conn)
        yield conn

    def begin(self) -> Any:
        return self._ctx()

    def connect(self) -> Any:
        return self._ctx()

    def dispose(self) -> None:
        self.disposed += 1


@pytest.fixture()
def pg_factory(monkeypatch):
    """造一个接假引擎的后端；`make(*scripts)` 返回 (store, engine, 建引擎的调用)。"""
    def make(*scripts: list[Any]):
        engine = _FakeEngine(*scripts)
        calls: list[tuple[str, dict[str, Any]]] = []

        def _create(url: str, **kw: Any) -> _FakeEngine:
            calls.append((url, kw))
            return engine

        monkeypatch.setattr("sqlalchemy.create_engine", _create)
        store = PostgresStateStore("postgresql+psycopg://u:p@db:5432/x")
        return store, engine, calls

    return make


# ---------------------------------------------------------------- 构造与关闭

def test_engine_built_with_pool_pre_ping_and_disposed(pg_factory):
    """建后端即配 `pool_pre_ping` + 三段超时：否则 PG 掐掉的空闲连接会变成随机报错。

    超时口径由 `storage.db.engine_kwargs()` 统一给出（libpq connect / 池 pool /
    服务端 statement），故这里只断言"关键项在"，并与 settings 现值对照——
    锁死整字典会让"多加一项超时"这种正当改动弄红测试。
    """
    from data_dog import settings

    store, engine, calls = pg_factory()
    url, kwargs = calls[0]
    assert url == "postgresql+psycopg://u:p@db:5432/x"
    assert kwargs["pool_pre_ping"] is True and kwargs["future"] is True
    assert kwargs["pool_timeout"] == settings.db_pool_timeout_s()
    assert kwargs["connect_args"]["connect_timeout"] == settings.db_connect_timeout_s()
    st_ms = settings.db_statement_timeout_ms()
    if st_ms:                                   # 0 = 不限语句，此时不下发 options
        assert f"statement_timeout={st_ms}" in kwargs["connect_args"]["options"]
    store.close()
    assert engine.disposed == 1
    print("PASS test_engine_built_with_pool_pre_ping_and_disposed")


# ---------------------------------------------------------------- mutate

def test_mutate_existing_row_single_transaction_and_compact_json(pg_factory):
    """已有行：同一事务内锁行→fn→写回；写入的是紧凑且保留中文的 JSON。"""
    store, engine, _ = pg_factory([("old",)], [])
    seen: list[Any] = []

    def fn(cur: Any) -> tuple[Any, Any]:
        seen.append(cur)
        return {"标签": "温度", "n": 1}, "the-result"

    assert store.mutate("dev:1", fn) == "the-result"     # 返回 fn 的 result
    assert seen == ["old"]                               # fn 收到锁内读到的值
    assert len(engine.conns) == 1                        # 锁 + fn + 写在同一事务

    calls = engine.conns[0].calls
    assert calls[0] == ("SELECT value FROM state_kv WHERE key = :k FOR UPDATE",
                        {"k": "dev:1"})
    sql, params = calls[-1]
    assert sql.startswith("UPDATE state_kv SET value = CAST(:v AS JSON)")
    assert params["k"] == "dev:1"
    # ensure_ascii=False + 无空格分隔：中文不膨胀成转义序列，落库体积可控
    assert params["v"] == '{"标签":"温度","n":1}'
    assert isinstance(params["t"], float)                 # updated_at 与锁同一时刻
    print("PASS test_mutate_existing_row_single_transaction_and_compact_json")


def test_mutate_first_time_inserts_placeholder_then_calls_fn_once(pg_factory):
    """首次（无行）：INSERT 占位 → 重取 → 空占位归一到 None；fn **只调一次**。

    只调一次是刻意设计（行锁已挡住并发，无需像 CAS 那样重跑）。若哪天复活重试
    循环，有副作用的 fn（计数、发通知）就会被执行两次——故这里钉死。
    """
    store, engine, _ = pg_factory([], [], [({},)])
    seen: list[Any] = []

    def fn(cur: Any) -> tuple[Any, Any]:
        seen.append(cur)
        return {"n": 1}, 7

    assert store.mutate("k", fn) == 7
    assert seen == [None]                # PG JSON 列读回的空占位 {} → 视为"不存在"
    assert len(engine.conns) == 1        # 建行与写入也在同一事务

    calls = engine.conns[0].calls
    assert calls[0] == ("SELECT value FROM state_kv WHERE key = :k FOR UPDATE",
                        {"k": "k"})
    assert "INSERT INTO state_kv (key, value, updated_at)" in calls[1][0]
    assert "ON CONFLICT (key) DO NOTHING" in calls[1][0]   # 并发建行不报唯一键冲突
    assert "VALUES (:k, '{}', :t)" in calls[1][0]
    assert calls[1][1]["k"] == "k" and isinstance(calls[1][1]["t"], float)
    assert calls[2] == ("SELECT value FROM state_kv WHERE key = :k FOR UPDATE",
                        {"k": "k"})
    print("PASS test_mutate_first_time_inserts_placeholder_then_calls_fn_once")


def test_mutate_row_absent_after_conflict_is_first_call(pg_factory):
    """极端情形：ON CONFLICT 后重取仍无行 → 也按"首次调用"处理（cur=None）。

    不能因为 `row is None` 就崩掉：并发删行/权限异常下这是可达状态，
    滥抛异常会让限流与归并整条链路随机失败。
    """
    store, engine, _ = pg_factory([], [], [])
    seen: list[Any] = []
    assert store.mutate("k", lambda c: (seen.append(c) or {"n": 1}, None)) is None
    assert seen == [None]
    assert len(engine.conns) == 1
    print("PASS test_mutate_row_absent_after_conflict_is_first_call")


# ---------------------------------------------------------------- 读

def test_get_normalizes_empty_object_and_missing(pg_factory):
    """`{}` 占位与"行不存在"对调用方必须是同一件事（None）。"""
    store, engine, _ = pg_factory([({"a": 1},)], [({},)], [])
    assert store.get("k") == {"a": 1}
    assert store.get("k") is None        # 空占位 → 未写入过
    assert store.get("k") is None        # 行不存在
    # 读不加锁、不进显式事务：每次 get 一条独立连接、只发一条 SQL
    assert len(engine.conns) == 3
    assert all(len(c.calls) == 1 for c in engine.conns)
    assert engine.conns[0].calls[0] == (
        "SELECT value FROM state_kv WHERE key = :k", {"k": "k"})
    print("PASS test_get_normalizes_empty_object_and_missing")


def test_keys_without_prefix_selects_all(pg_factory):
    """空前缀走全表 SELECT（不带 LIKE），避免 PG 上多一次无谓的模式匹配。"""
    store, engine, _ = pg_factory([("a",), ("b",)])
    assert store.keys() == ["a", "b"]
    assert engine.conns[0].calls[0] == ("SELECT key FROM state_kv", {})
    print("PASS test_keys_without_prefix_selects_all")


def test_keys_prefix_uses_escaped_like_pattern(pg_factory):
    """前缀必须转义为 LIKE 模式：否则 `dev_1` 会命中 `devX1`（读到别人的键）。"""
    store, engine, _ = pg_factory([("dev_1",)])
    assert store.keys("dev_1%\\") == ["dev_1"]

    sql, params = engine.conns[0].calls[0]
    assert sql == "SELECT key FROM state_kv WHERE key LIKE :p ESCAPE '\\'"
    assert params["p"] == _like_prefix("dev_1%\\")
    print("PASS test_keys_prefix_uses_escaped_like_pattern")


def test_delete_scopes_to_single_key(pg_factory):
    """删除只按 key 定位，不做前缀匹配（误删会静默清掉别人的限流桶）。"""
    store, engine, _ = pg_factory([()])
    store.delete("k")
    assert engine.conns[0].calls[0] == (
        "DELETE FROM state_kv WHERE key = :k", {"k": "k"})
    print("PASS test_delete_scopes_to_single_key")


# ---------------------------------------------------------------- 纯函数

def test_dump_is_compact_and_keeps_unicode():
    """落库 JSON 紧凑且保留中文（ascii 转义会让中文键值膨胀数倍）。"""
    assert _dump({"a": 1, "b": "温度"}) == '{"a":1,"b":"温度"}'
    assert _dump(None) == "null"
    assert json.loads(_dump({"nested": [1, {"x": None}]})) == {"nested": [1, {"x": None}]}
    print("PASS test_dump_is_compact_and_keeps_unicode")


def test_like_prefix_escapes_exactly_three_metacharacters():
    """`%`/`_`/反斜杠三个 LIKE 元字符的转义顺序：反斜杠先加倍，否则会吃自己。"""
    assert _like_prefix("dev") == "dev%"
    assert _like_prefix("a_b") == "a\\_b%"
    assert _like_prefix("a%b") == "a\\%b%"
    assert _like_prefix("a\\b") == "a\\\\b%"
    print("PASS test_like_prefix_escapes_exactly_three_metacharacters")


# ---------------------------------------------------------------- 真库（默认跳过）

@pytest.mark.postgres
@pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 集成测试")
def test_keys_prefix_and_empty_object_on_real_pg():
    """真库上验两件假引擎证明不了的事：`ESCAPE '\\'` PG 确实认；JSON 列 `{}` 读回是 dict。

    用**独立 schema**（与 test_storage_postgres.py 同法）隔离，避免与并行的 PG
    用例互相 TRUNCATE state_kv。
    """
    from sqlalchemy import create_engine, text

    from data_dog.storage import migrations

    schema = f"l2a_{uuid.uuid4().hex[:12]}"
    admin = create_engine(_PG_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{schema}"'))
    sep = "&" if "?" in _PG_URL else "?"
    url = f"{_PG_URL}{sep}options=-csearch_path%3D{schema}"
    store = PostgresStateStore(url)
    try:
        migrations.upgrade_head(url)
        tag = f"l2a_{uuid.uuid4().hex[:8]}"
        for k in (f"{tag}_1", f"{tag}X1", f"{tag}%2", f"{tag}Y2"):
            store.mutate(k, lambda cur: ({"n": 1}, None))
        assert sorted(store.keys(f"{tag}_")) == [f"{tag}_1"]     # `_` 没被当通配符
        assert sorted(store.keys(f"{tag}%")) == [f"{tag}%2"]     # `%` 没被当通配符

        empty = f"{tag}:empty"
        store.mutate(empty, lambda cur: ({}, None))
        seen: list[Any] = []
        store.mutate(empty, lambda cur: (seen.append(cur) or {}, None))
        assert seen == [None], "真库上 JSON 列的 {} 必须归一到 None"
    finally:
        store.close()
        with admin.connect() as c:
            c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
    print("PASS test_keys_prefix_and_empty_object_on_real_pg")
