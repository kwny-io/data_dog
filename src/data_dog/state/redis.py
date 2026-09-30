"""Redis 后端：多副本共享状态（限流检查成为热路径瓶颈时的选择）。

原子性实现
----------
用 Redis 原生的 **WATCH/MULTI/EXEC 乐观锁**做 read-modify-write：
WATCH key → 读值 → 在本地算 fn → MULTI 写回 → EXEC。若在我们 WATCH 之后有
别的客户端动了这个 key，EXEC 返回 None（事务被打断）→ 我们**重新读取并重跑
fn**。这正是 `mutate` 契约要求的"冲突则重试 fn"。

为什么乐观锁而非 Lua 脚本：fn 是任意 Python（补令牌、按时间窗合并分组），
无法在 Lua 里执行。乐观重试在按设备/部件的 key 上冲突率低（不同 key 不互相
干扰），重试极少发生。

为什么限流特别适合 Redis：每条遥测都要一次 check-and-decrement，PG 的每条
一次 DB 往返会成为热路径；Redis 原生 TTL + 内存原子操作是 sub-ms。窗口/候选
分组更新频率低、要耐久，PG 更合适——所以两个后端共用同一接口，按需混用。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .base import StateStore

_MAX_RETRIES = 8


class RedisStateStore(StateStore):
    def __init__(self, url: str = "", *, client: Any = None) -> None:
        if client is not None:
            self._client = client          # 测试注入（如 fakeredis）
        else:
            import redis  # 延迟导入：无 redis 依赖时不影响其它后端
            self._client = redis.Redis.from_url(url, decode_responses=True)

    def _load(self, key: str) -> Any:
        raw = self._client.get(key)
        if raw is None:
            return None
        val = json.loads(raw)
        return None if val == {} else val

    def mutate(self, key: str, fn: Callable[[Any], tuple[Any, Any]]) -> Any:
        client = self._client
        for _ in range(_MAX_RETRIES):
            with client.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    cur = self._load(key)
                    new_value, result = fn(cur)
                    pipe.multi()
                    pipe.set(key, json.dumps(new_value, ensure_ascii=False,
                                             separators=(",", ":")))
                    pipe.execute()
                    return result
                except Exception as e:      # WatchError 或 JSON 异常
                    # 事务被打断（并发写了同一 key）→ 重试；其它异常上抛。
                    if type(e).__name__ != "WatchError":
                        raise
                finally:
                    pipe.reset()
        raise RuntimeError(f"Redis 状态更新冲突重试 {_MAX_RETRIES} 次仍失败：{key}")

    def get(self, key: str) -> Any:
        return self._load(key)

    def keys(self, prefix: str = "") -> list[str]:
        # 用 SCAN 游标迭代而非 KEYS：KEYS 会阻塞整个 Redis（大库上是生产事故）。
        pattern = f"{prefix}*" if prefix else "*"
        out: list[str] = []
        cursor = 0
        while True:
            cursor, batch = self._client.scan(cursor=cursor, match=pattern,
                                              count=500)
            out.extend(batch)
            if cursor == 0:
                break
        return out

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:                    # noqa: BLE001  某些 client 无 close
            pass
