"""P3 测试 SaaS Adapter（对齐架构 §21.1 / §19.3 验收路径）。

模拟 SaaS 事件系统：接收正式事件、返回确认 Receipt、并对反馈（误报/维修结果）留档。
只接收**业务所需摘要 + 受控证据 Receipt**，不含沙箱路径/堆栈/密钥/原始载荷（§12.5）。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

_FORBIDDEN_KEYS = {"payload_ref", "raw_payload", "sandbox_path", "stack", "secret",
                   "api_key", "model_reasoning"}


def _scrub(d: dict[str, Any]) -> dict[str, Any]:
    """剔除禁止外发的字段（递归）。"""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if k in _FORBIDDEN_KEYS:
            continue
        out[k] = _scrub(v) if isinstance(v, dict) else v
    return out


@dataclass
class SaaSTestAdapter:
    """本地假 SaaS：记录收到的事件与反馈，返回幂等 Receipt。"""
    received: list[dict[str, Any]] = field(default_factory=list)
    feedback: list[dict[str, Any]] = field(default_factory=list)
    _seen: dict[str, str] = field(default_factory=dict)   # dedup_id -> receipt
    fail_times: int = 0                                   # >0 时先失败 N 次（测重试）

    def deliver(self, payload: dict[str, Any], *, idempotency_key: str,
                revision: int) -> tuple:
        dedup = hashlib.sha256(
            f"{idempotency_key}#{revision}".encode()).hexdigest()[:12]
        # 幂等：同一 (key, revision) 重复投递 → 返回同一 Receipt，不重复入库
        if dedup in self._seen:
            return True, {"receipt_id": self._seen[dedup], "duplicate": True}
        if self.fail_times > 0:
            self.fail_times -= 1
            return False, {"error": "saas_unavailable"}
        clean = _scrub(payload)
        clean["_received_at"] = time.time()
        self.received.append(clean)
        receipt = "saas_receipt_" + dedup
        self._seen[dedup] = receipt
        return True, {"receipt_id": receipt, "duplicate": False}

    # -- 人工/维修反馈回填（§6.5 / §21.1）--
    def submit_feedback(self, event_id: str, kind: str, note: str = "") -> dict[str, Any]:
        """kind: false_positive(误报) | repaired(已修) | no_action(无需处理)"""
        if kind not in ("false_positive", "repaired", "no_action"):
            return {"ok": False, "error": "invalid_input", "detail": f"未知反馈类型: {kind}"}
        fb = {"event_id": event_id, "kind": kind, "note": note, "at": time.time()}
        self.feedback.append(fb)
        return {"ok": True, **fb}
