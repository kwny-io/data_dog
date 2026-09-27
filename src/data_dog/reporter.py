"""A3：提出「发布 / 回滚 / 上报」意图（架构 13.2 的 write 类工具，最小可跑版）。

架构要求 Agent 不能直接干副作用（发布、上报 SaaS），而应**提出意图**，
由确定性控制面去执行，并返回一个可审计的 **Receipt（回执）**。

这里做一个最小、本地、可理解的实现：
- 一个 `ProposeOutbox`（本地假 outbox）接收这些"意图"；
- 三个 propose_* 函数写入 outbox，按**幂等键去重**（不重复提出），
  确认写成功才返回 Receipt；
- **不接真实 SaaS / 控制面**——只记录"想把什么上报出去"，闭环停在意图层。

对应 schema：Receipt 结构贴近架构 5 的 "Receipt" 定义（确认成功的最小回执）。
"""
from __future__ import annotations

import builtins
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from .errors import ToolErrorCode, err, ok


@dataclass
class OutboxItem:
    """outbox 里的一条待处理意图。"""
    idempotency_key: str      # 幂等键：同一意图只收一次
    op: str                   # release / rollback / alert_report
    payload: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    receipt_id: str = field(default="")

    def __post_init__(self):
        # receipt_id 是内容寻址的，同幂等键 → 同 receipt，天然幂等
        self.receipt_id = "receipt_" + hashlib.sha256(
            (self.op + ":" + self.idempotency_key).encode()
        ).hexdigest()[:12]


class ProposeOutbox:
    """本地假 outbox：接收 propose_* 意图，按幂等键去重。

    生产环境这个类会换成"对接 SaaS / 控制面"的实现，接口保持不变。
    """

    def __init__(self):
        self._items: dict[str, OutboxItem] = {}

    def put(self, op: str, idempotency_key: str, payload: dict[str, Any]) -> OutboxItem:
        """写入一条意图。同 idempotency_key 已存在 → 直接返回已有条目（幂等）。"""
        if idempotency_key in self._items:
            return self._items[idempotency_key]
        item = OutboxItem(op=op, idempotency_key=idempotency_key, payload=payload)
        self._items[idempotency_key] = item
        return item

    def list(self) -> builtins.list[dict[str, Any]]:
        return [item.__dict__ for item in self._items.values()]

    def __len__(self) -> int:
        return len(self._items)


# 全局单例（进程内共享，便于多候选一轮内去重）
_OUTBOX = ProposeOutbox()


def get_outbox() -> ProposeOutbox:
    return _OUTBOX


def _receipt(item: OutboxItem) -> dict[str, Any]:
    """最小的、可审计的回执：确认意图已成功落库。"""
    return {
        "op": item.op,
        "idempotency_key": item.idempotency_key,
        "receipt_id": item.receipt_id,
        "status": "acknowledged",
        "duplicate": False,   # 由调用处置为 True 表示这是重复请求
    }


def propose_operator_release(
    artifact_ref: str, target_stage: str, reason: str, outbox: ProposeOutbox | None = None,
) -> dict[str, Any]:
    """提出：把一个算子制品发布到某阶段（影子/灰度/全量）。"""
    box = _OUTBOX if outbox is None else outbox
    if not artifact_ref or not target_stage:
        return err(ToolErrorCode.INVALID_INPUT, "artifact_ref 与 target_stage 不能为空")
    key = f"release:{artifact_ref}:{target_stage}"
    existing = box._items.get(key)
    item = box.put("operator_release", key, {
        "artifact_ref": artifact_ref, "target_stage": target_stage, "reason": reason,
    })
    rc = _receipt(item)
    rc["duplicate"] = existing is not None
    return ok(rc)


def propose_operator_rollback(
    release_ref: str, reason: str, outbox: ProposeOutbox | None = None,
) -> dict[str, Any]:
    """提出：回滚某个已发布的制品（恢复前一签名版本）。"""
    box = _OUTBOX if outbox is None else outbox
    if not release_ref:
        return err(ToolErrorCode.INVALID_INPUT, "release_ref 不能为空")
    key = f"rollback:{release_ref}"
    existing = box._items.get(key)
    item = box.put("operator_rollback", key, {
        "release_ref": release_ref, "reason": reason,
    })
    rc = _receipt(item)
    rc["duplicate"] = existing is not None
    return ok(rc)


def propose_alert_report(
    investigation_ref: str, summary: str, severity: str,
    outbox: ProposeOutbox | None = None,
) -> dict[str, Any]:
    """提出：把一份调查结论作为正式事件上报（进入 SaaS 生命周期前的一步）。"""
    box = _OUTBOX if outbox is None else outbox
    if not investigation_ref:
        return err(ToolErrorCode.INVALID_INPUT, "investigation_ref 不能为空")
    key = f"alert_report:{investigation_ref}"
    existing = box._items.get(key)
    item = box.put("alert_report", key, {
        "investigation_ref": investigation_ref,
        "summary": summary, "severity": severity,
    })
    rc = _receipt(item)
    rc["duplicate"] = existing is not None
    return ok(rc)
