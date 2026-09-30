"""降级队列后端：把 §9.4 的"有界待办队列"从进程内 list 抽象为可插拔后端。

为什么需要它
------------
`degradation.py` 的 `DegradationPolicy` 原本把待复核候选存在**进程内 list**——
进程重启即丢，多副本各持一份。真实运维里"Agent 掉线期间攒下的候选"正是最不能
丢的东西（它们是差点被漏掉的告警）。本模块把这个队列外置。

设计对齐 `state/base.py` 的可插拔思路
------------------------------------
- `QueueBackend` 只是**一个协议**（push/drain/len/reset/items），不含决策逻辑。
- `InMemoryQueue`：**默认**，与旧行为逐字节一致（单副本/测试/离线演示）。
- `StateStoreQueue`：复用 `StateStore`（`mutate` 原子 read-modify-write），
  队列存共享状态 → **重启可续 + 多副本共享**（顺带解决多副本各持一份队列）。
- 接入真 MQ（Kafka / NATS / Redis Stream）时再实现一个后端即可，决策层不动。

容量为何传进 `push`
-------------------
旧代码在决策函数里 `len(queue) >= capacity` 判断，多副本下各算各的长度会**超发**。
故把「查长度 + 追加」做成 `push` 内的**一步原子操作**：满则返回 False，调用方
退化为直接上报。`StateStoreQueue` 借 `mutate` 的原子性天然满足。

诚实边界（同 gap 文档 §6）
--------------------------
- **持久 ≠ 消息队列**：`StateStoreQueue` 是共享状态，**不是** Kafka——无投递确认、
  无消费组、无分区。真 MQ 语义需换后端；接口已为此留位。
- **队列项是 candidate_id 引用**：恢复后重放依赖**候选已落库**（真相在 candidates 表）。
- **退避/重试不在决策层**：`attempts` 字段只做记录，退避属传输层。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..state.base import StateStore

# 共享状态里的队列 key（与 G3 快照同源，可被 checkpoint 覆盖）
QUEUE_STATE_KEY = "degradation:queue"


@dataclass
class QueueItem:
    """队列项：持久化需自描述（旧版只存 candidate_id 字符串）。"""
    candidate_id: str
    severity: str = ""
    enqueued_at: float = 0.0
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "severity": self.severity,
            "enqueued_at": self.enqueued_at,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> QueueItem:
        """宽容解析：缺字段用默认值（跨版本恢复时不因少字段而崩）。"""
        return cls(
            candidate_id=str(d.get("candidate_id", "")),
            severity=str(d.get("severity", "")),
            enqueued_at=float(d.get("enqueued_at", 0.0) or 0.0),
            attempts=int(d.get("attempts", 0) or 0),
        )


class QueueBackend(Protocol):
    """有界待办队列后端。所有方法须对并发调用安全。"""

    def push(self, item: QueueItem, *, capacity: int) -> bool:
        """原子地「查长度 + 追加」；满（>= capacity）返回 False。"""
        ...

    def drain(self) -> list[QueueItem]:
        """原子地取出**并清空**队列（Agent 恢复后取回待复核候选）。"""
        ...

    def __len__(self) -> int:
        ...

    def items(self) -> list[QueueItem]:
        """当前队列内容（副本，只读）。"""
        ...

    def reset(self) -> None:
        """清空队列（测试/演示用）。"""
        ...


@dataclass
class InMemoryQueue:
    """默认后端：进程内 list，语义与旧 `_queue` 逐字节一致。"""

    _items: list[QueueItem] = field(default_factory=list, repr=False)

    def push(self, item: QueueItem, *, capacity: int) -> bool:
        if len(self._items) >= capacity:
            return False
        self._items.append(item)
        return True

    def drain(self) -> list[QueueItem]:
        out = list(self._items)
        self._items.clear()
        return out

    def __len__(self) -> int:
        return len(self._items)

    def items(self) -> list[QueueItem]:
        return list(self._items)

    def reset(self) -> None:
        self._items.clear()


class StateStoreQueue:
    """持久后端：把队列存进 `StateStore`（重启可续 + 多副本共享）。

    存的是 JSON 列表（`[QueueItem.to_dict(), ...]`）；`mutate` 保证 push/drain
    的读改写原子，故并发/多副本下不丢项、不重复取。
    """

    def __init__(self, state: StateStore, *, key: str = QUEUE_STATE_KEY) -> None:
        self._state = state
        self._key = key

    @staticmethod
    def _decode(raw: Any) -> list[QueueItem]:
        if not isinstance(raw, list):
            return []
        return [QueueItem.from_dict(d) for d in raw if isinstance(d, dict)]

    def push(self, item: QueueItem, *, capacity: int) -> bool:
        def fn(cur: Any) -> tuple[Any, Any]:
            raw = cur if isinstance(cur, list) else []
            if len(raw) >= capacity:
                return raw, False
            return [*raw, item.to_dict()], True

        return bool(self._state.mutate(self._key, fn))

    def drain(self) -> list[QueueItem]:
        def fn(cur: Any) -> tuple[Any, Any]:
            return [], self._decode(cur)

        return list(self._state.mutate(self._key, fn))

    def __len__(self) -> int:
        raw = self._state.get(self._key)
        return len(raw) if isinstance(raw, list) else 0

    def items(self) -> list[QueueItem]:
        return self._decode(self._state.get(self._key))

    def reset(self) -> None:
        def fn(_cur: Any) -> tuple[Any, Any]:
            return [], None

        self._state.mutate(self._key, fn)


def default_queue_backend() -> QueueBackend:
    """按 `DD_DEGRADATION_QUEUE` 造后端：memory（默认）/ state（持久共享）。

    诚实边界：state 后端要真跨进程持久/共享，`DD_STATE_BACKEND` 须落在
    postgres/redis（memory 后端仅在**同进程**内共享，模拟"重启"够用、真重启不够）。
    """
    from .. import settings
    from ..state import get_state_store

    if settings.degradation_queue_backend() == "state":
        return StateStoreQueue(get_state_store())
    return InMemoryQueue()


def summarize_items(items: list[QueueItem]) -> list[dict[str, Any]]:
    """调试/演示用：队列项的可读摘要。"""
    return [it.to_dict() for it in items]


# 便于外部构造自描述项（默认取当前墙钟时间）
def make_item(candidate_id: str, severity: str = "", *,
              now: float | None = None) -> QueueItem:
    return QueueItem(candidate_id=candidate_id, severity=severity,
                     enqueued_at=time.time() if now is None else now)


__all__ = [
    "QUEUE_STATE_KEY",
    "InMemoryQueue",
    "QueueBackend",
    "QueueItem",
    "StateStoreQueue",
    "default_queue_backend",
    "make_item",
    "summarize_items",
]
