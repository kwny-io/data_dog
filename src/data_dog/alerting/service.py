# -*- coding: utf-8 -*-
"""P3 上报服务：把 Agent 调查结论走完"策略门 → 事件 → 投递 SaaS"闭环。

    InvestigationReport + signals
        → ReportPolicyGate（确定性决策）
        → AlertEvent（新建 or 更新已有 Episode）
        → AlertOutbox（幂等投递）
        → SaaSTestAdapter（接收 + Receipt）

Episode 语义（§14.3）：同一逻辑故障用**同一 event_id** 与**同一幂等键**；
状态更新只递增 `event_revision`，不新建事件、不重复投递。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from .lifecycle import ACTIVE_STATES, AlertEvent, AlertLifecycle
from .outbox import AlertOutbox, episode_key_from, make_idempotency_key
from .policy import POLICY_VERSION, PolicyDecision, ReportPolicyGate
from .saas import SaaSTestAdapter


def _event_id(device_ref: str, event_type: str, episode_key: str) -> str:
    h = hashlib.sha256(f"{device_ref}:{event_type}:{episode_key}".encode()).hexdigest()
    return "alert_" + h[:16]


@dataclass
class ReportOutcome:
    decision: PolicyDecision
    event: Optional[AlertEvent] = None
    delivered: bool = False
    receipt: str = ""
    duplicate: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.to_dict(),
            "event_id": self.event.event_id if self.event else None,
            "event_state": self.event.state if self.event else None,
            "event_revision": self.event.event_revision if self.event else None,
            "severity": self.event.severity if self.event else None,
            "delivered": self.delivered, "receipt": self.receipt,
            "duplicate": self.duplicate,
        }


class AlertReportingService:
    def __init__(self, *, gate: Optional[ReportPolicyGate] = None,
                 outbox: Optional[AlertOutbox] = None,
                 adapter: Optional[SaaSTestAdapter] = None):
        self.gate = gate or ReportPolicyGate()
        self.outbox = outbox or AlertOutbox()
        self.adapter = adapter or SaaSTestAdapter()
        self._episodes: Dict[str, AlertLifecycle] = {}      # 幂等键 → 生命周期

    # ---- 主流程 ----
    def process(self, report: Dict[str, Any],
                signals: Optional[Dict[str, Any]] = None,
                *, device_ref: str = "device-opaque",
                event_type: Optional[str] = None,
                component_ref: Optional[str] = None) -> ReportOutcome:
        s = dict(signals or {})
        event_type = event_type or report.get("event_type") or "unknown_event"
        component_ref = component_ref or (report.get("candidate_refs") or ["unknown"])[0]
        episode_key = episode_key_from(str(report.get("investigation_id", "")), component_ref)

        # 若该 Episode 已有活动事件 → 告诉策略门"走更新"
        ikey = make_idempotency_key(device_ref, event_type, episode_key)
        existing = self._episodes.get(ikey)
        if existing and existing.is_active():
            s["has_active_episode"] = True

        decision = self.gate.evaluate(report, s)

        if not decision.report:
            return ReportOutcome(decision=decision)

        # ---- 新建 or 更新事件 ----
        if existing is None:
            event = AlertEvent(
                event_id=_event_id(device_ref, event_type, episode_key),
                idempotency_key=ikey, device_ref=device_ref,
                component_ref=component_ref, event_type=event_type,
                state="detected", severity=decision.severity,
                confidence=float(report.get("confidence", 0.0) or 0.0),
                detected_at=str(report.get("detected_at", "")),
                summary=str(report.get("summary", "")),
                recommended_actions=list(report.get("recommended_actions", []) or []),
                investigation_id=str(report.get("investigation_id", "")),
                policy_version=decision.policy_version,
            )
            lc = AlertLifecycle(event)
            self._episodes[ikey] = lc
            # 策略门通过即 Confirmed（若含厂商严重码则直接升级到 escalated 由调用方决定）
            lc.transition("confirmed", reason="策略门通过")
        else:
            lc = existing
            event = lc.event
            event.severity = decision.severity
            event.summary = str(report.get("summary", "")) or event.summary
            # 更新但不改状态（除非调用方显式要求升级）；revision 递增以体现"更新"
            event.event_revision += 1

        # ---- 幂等投递 ----
        entry = self.outbox.enqueue(event.idempotency_key, event.event_revision,
                                    self._payload(event, decision))
        was_new = entry.status == "pending" and entry.attempts == 0
        res = self.outbox.deliver(self.adapter)
        entry = self.outbox._entries[entry.dedup_id]     # 取回更新后的状态
        return ReportOutcome(
            decision=decision, event=event,
            delivered=(entry.status == "delivered"),
            receipt=entry.receipt,
            duplicate=not was_new,
        )

    # ---- 状态操作（供人工/自动流程调用）----
    def transition(self, idempotency_key: str, to_state: str, *, reason: str = "") -> AlertEvent:
        lc = self._episodes.get(idempotency_key)
        if lc is None:
            raise KeyError(f"未知 Episode: {idempotency_key}")
        return lc.transition(to_state, reason=reason)

    def active_events(self) -> Dict[str, str]:
        return {k: v.event.state for k, v in self._episodes.items() if v.is_active()}

    @staticmethod
    def _payload(event: AlertEvent, decision: PolicyDecision) -> Dict[str, Any]:
        return {
            "event_id": event.event_id,
            "event_revision": event.event_revision,
            "idempotency_key": event.idempotency_key,
            "device_ref": event.device_ref,
            "component_ref": event.component_ref,
            "event_type": event.event_type,
            "state": event.state,
            "severity": event.severity,
            "confidence": event.confidence,
            "detected_at": event.detected_at,
            "summary": event.summary,
            "recommended_actions": event.recommended_actions,
            "evidence_receipt": event.evidence_receipt or "evidence-receipt://opaque",
            "source": {"investigation_id": event.investigation_id,
                       "policy_version": decision.policy_version},
        }
