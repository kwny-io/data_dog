"""流式源的单消费者租约（选主）——file 源多副本不重复消费的落点。

为什么需要它
------------
Kafka 有消费者组、MQTT 有共享订阅，都由 broker 协调"一条消息只给一个副本"。
**单个本地文件没有 broker**：多副本各读各的偏移 → 同一事件被每个副本各算一遍，
候选重复、限流口径翻倍。本源用**应用层租约**选出一个持有者，同一时刻只有它读文件。

原子性从哪来
------------
租约存在共享状态（`StateStore`）里，靠既有的 `mutate(key, fn)` 原子原语：
"读当前 → 判断无主/过期 → 抢占或失败"在**一个原子操作**内完成，多副本并发
抢占只会有一个成功。用 memory 后端则退化为进程内选主（单副本场景已足够）。

时钟与边界（诚实说明）
--------------------
- 用**墙钟** `time.time()` 而非 `monotonic`：租约要在**不同进程/机器**之间比较
  过期时间，`monotonic` 是各进程私有的，跨副本不可比。代价是**依赖各副本时钟大致
  同步**——时钟漂移过大时可能误判过期（提前接管或续不上）。生产须跑 NTP。
- **接管有窗口**：持有者崩溃到租约过期之间（至多 `ttl_s`）该源无人读；file 是
  持久化的，续读即可，**不丢数据**。
- 租约是**协作式**：它只保证"同一时刻只有一个自愿的持有者"，不能强制杀死一个
  不守规矩的旧持有者。持有者必须**每轮调 `renew`**，否则租约会过期被旁人抢走。
"""
from __future__ import annotations

import os
import socket
import time
from collections.abc import Callable
from typing import Any

from ..state.base import StateStore


def default_owner() -> str:
    """默认持有者标识：host + pid（同一机器多副本也能区分）。"""
    return f"{socket.gethostname()}:{os.getpid()}"


class Lease:
    """一个具名租约：持有者定期续约，过期后可被抢占。"""

    def __init__(self, state: StateStore, key: str, owner: str, *,
                 ttl_s: float = 30.0,
                 clock: Callable[[], float] = time.time) -> None:
        self._state = state
        self._key = key
        self._owner = owner
        self._ttl = max(1.0, float(ttl_s))
        self._clock = clock

    @property
    def key(self) -> str:
        return self._key

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def state(self) -> StateStore:
        return self._state

    def _record(self, when: float) -> dict[str, Any]:
        return {"owner": self._owner, "expires_at": when}

    def acquire(self) -> bool:
        """抢占租约：无主 / 已过期 / 本就是我 → 归我；否则失败。"""
        now = self._clock()
        ttl = self._ttl

        def fn(cur: Any) -> tuple[Any, Any]:
            if not isinstance(cur, dict):
                return self._record(now + ttl), True
            if cur.get("owner") == self._owner:
                return self._record(now + ttl), True           # 已持有 → 续期
            if float(cur.get("expires_at", 0) or 0) <= now:
                return self._record(now + ttl), True           # 过期 → 抢占
            return cur, False                                  # 他人有效持有

        return bool(self._state.mutate(self._key, fn))

    def renew(self) -> bool:
        """仅在我当前持有租约时续期；**不会**抢占他人（抢占用 `acquire`）。"""
        now = self._clock()
        ttl = self._ttl

        def fn(cur: Any) -> tuple[Any, Any]:
            if isinstance(cur, dict) and cur.get("owner") == self._owner:
                return self._record(now + ttl), True
            return cur, False

        return bool(self._state.mutate(self._key, fn))

    def release(self) -> None:
        """主动释放：把租约置为无主且立即过期，让旁人可即刻接管。"""
        def fn(cur: Any) -> tuple[Any, Any]:
            if isinstance(cur, dict) and cur.get("owner") == self._owner:
                return {"owner": "", "expires_at": 0.0}, None
            return cur, None

        self._state.mutate(self._key, fn)

    def holder(self) -> str:
        """当前持有者标识（无主/已过期返回空串）。"""
        rec = self._state.get(self._key)
        if not isinstance(rec, dict) or not rec.get("owner"):
            return ""
        if float(rec.get("expires_at", 0) or 0) <= self._clock():
            return ""
        return str(rec["owner"])

    def is_held_by_me(self) -> bool:
        return self.holder() == self._owner


__all__ = ["Lease", "default_owner"]
