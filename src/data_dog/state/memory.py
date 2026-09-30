"""进程内后端：单副本 / 测试 / 离线演示的默认。

语义与共享后端**刻意保持一致**：`mutate` 在锁内做 read-modify-write，因此
同一进程内的并发调用器具原子性——测试用它可以真实覆盖"并发扣令牌不超发"
这类断言，而无需真起 PG/Redis。

诚实边界：状态只在**本进程**。多副本各自一份 → 限流和候选合并会各算各的。
生产多副本必须选 postgres/redis（见 factory 的告警）。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .base import StateStore


class InMemoryStateStore(StateStore):
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        # 单一全局锁：mutate 是短临界区（无 I/O），粗锁足够且无死锁风险。
        self._lock = threading.Lock()

    def mutate(self, key: str, fn: Callable[[Any], tuple[Any, Any]]) -> Any:
        with self._lock:
            cur = self._data.get(key)
            new_value, result = fn(cur)
            self._data[key] = new_value
            return result

    def get(self, key: str) -> Any:
        with self._lock:
            return self._data.get(key)

    def keys(self, prefix: str = "") -> list[str]:
        with self._lock:
            return [k for k in self._data if k.startswith(prefix)]

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)
