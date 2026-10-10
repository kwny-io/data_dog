"""进程内后端：单副本 / 测试 / 离线演示的默认。

语义与共享后端**刻意保持一致**：`mutate` 在锁内做 read-modify-write，因此
同一进程内的并发调用器具原子性——测试用它可以真实覆盖"并发扣令牌不超发"
这类断言，而无需真起 PG/Redis。

诚实边界：状态只在**本进程**。多副本各自一份 → 限流和候选合并会各算各的。
生产多副本必须选 postgres/redis（见 factory 的告警）。
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import Any

from .base import StateStore

# "该 key 本来不存在"的哨兵：区别于"存在且值为 None"（`mutate` 允许写回 None）。
_MISSING = object()


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

    def mutate_many(self, items: Sequence[tuple[str, Callable[[Any], tuple[Any, Any]]]]
                    ) -> list[Any]:
        """整批在**一次持锁**内完成，且**整批原子**（抛错则整批回滚）。

        进程内本就没有提交开销，这里保的是**语义一致**：PG 靠事务回滚、Redis 靠
        "EXEC 未执行"，内存后端若中途抛错就会留下半批——那会让"换后端是配置动作"
        这句话变成假的（见 `tests/test_state_store.py` 的跨后端契约断言）。
        """
        if not items:
            return []
        with self._lock:
            results: list[Any] = []
            undo: list[tuple[str, Any]] = []
            try:
                for key, fn in items:
                    undo.append((key, self._data.get(key, _MISSING)))
                    new_value, result = fn(self._data.get(key))
                    self._data[key] = new_value
                    results.append(result)
            except BaseException:
                for key, old in reversed(undo):
                    if old is _MISSING:
                        self._data.pop(key, None)
                    else:
                        self._data[key] = old
                raise
            return results

    def get(self, key: str) -> Any:
        with self._lock:
            return self._data.get(key)

    def keys(self, prefix: str = "") -> list[str]:
        with self._lock:
            return [k for k in self._data if k.startswith(prefix)]

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)
