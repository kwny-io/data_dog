"""把 P3 上报闭环**接进 HTTP 服务**（落库版）。

为什么需要它（缺口：服务从不产告警）
------------------------------------
`alerting/` 的 `AlertReportingService` 是**演示级**实现：Episode 状态放在进程内
`dict`、outbox 放内存列表。它跑得通、可测，但作为**长期服务**有两个真问题：
  1. 重启即丢——进程退出后活动 Episode 与待投递 outbox 全部消失；
  2. 多副本不共享——两个实例各自记一半 Episode，幂等键形同虚设。

更要命的是：**HTTP 服务此前根本没调用它**。`POST /v1/investigations` 跑完调查、
落库报告就返回了，从不到策略门。于是 `/v1/alerts`、`/v1/feedback`、`/v1/summary`
的 `alerts_by_state` 在真实运行里**永远是空的**——反馈端点看着接了，其实悬空。

本模块补上这一环：调查结论 → 策略门 → 正式事件 → outbox，**全部落库**。

与演示版的关系：**判定逻辑完全复用**（同一个 `ReportPolicyGate`、同一张
`TRANSITIONS`、同一套幂等键构成规则），差别只在**状态存放位置**（DB 而非内存）。
event_id / 幂等键仍由三要素**确定性派生**，因此同一 Episode 反复调查得到同一
event_id → 是**更新**（revision 递增）而非新建——这正是多副本安全的关键：
两个实例并发处理同一 Episode，落库 upsert 会收敛到同一行。

诚实边界
--------
- 投递目标仍是 `SaaSTestAdapter`（本地测试 SaaS）——项目里**没有真实 SaaS**。
  接进来的意义是把"调查→事件→投递→receipt"整链**持久化并可查**，不是宣称
  已对接生产系统。替换投递目标只需换 adapter。
- 本模块**不做重试调度**：投递失败只落 `pending`/失败态，重投由外部 worker
  负责（演示级 adapter 基本不会失败）。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..alerting.lifecycle import ACTIVE_STATES, AlertEvent, AlertLifecycle
from ..alerting.outbox import (
    episode_key_from,
    make_device_ref,
    make_event_id,
    make_idempotency_key,
)
from ..alerting.policy import ReportPolicyGate
from ..alerting.saas import SaaSTestAdapter
from ..schemas.events import CandidateEvent
from ..storage import repository as repo

# 厂商"保护类"规则 → 上报策略里的严重错误码（§14.2 条件①）。
# 当前仅已知一条（电机过温保护）。新增厂商规则时在此登记，**不做猜测性映射**。
_RULE_TO_VENDOR_CODE = {
    "MOTOR_TEMP_PROTECT": "OVERHEAT",
}


def vendor_codes_from_rules(rule_matches: list[str]) -> list[str]:
    """候选命中的厂商保护规则 → 严重错误码集合（供策略门条件①）。"""
    return sorted({_RULE_TO_VENDOR_CODE[r] for r in (rule_matches or [])
                   if r in _RULE_TO_VENDOR_CODE})


def build_signals(cand: CandidateEvent) -> dict[str, Any]:
    """从候选组装策略门所需的补充信号（证据引用 + 严重度 + 厂商严重码）。"""
    s: dict[str, Any] = {
        "evidence_refs": list(cand.evidence_refs) or [f"metricwin://{cand.candidate_id}"],
        "candidate_severities": [cand.severity_hint],
    }
    codes = vendor_codes_from_rules(list(cand.rule_matches or []))
    if codes:
        s["vendor_severe_codes"] = codes
    return s


def _row_to_alert_event(row: dict[str, Any]) -> AlertEvent:
    """落库的告警行 → `AlertEvent`（用于基于既有行做更新）。"""
    return AlertEvent(
        event_id=row["event_id"],
        idempotency_key=row.get("idempotency_key", ""),
        device_ref=row.get("device_ref", ""),
        component_ref=row.get("component_ref", ""),
        event_type=row.get("event_type", ""),
        state=row.get("state", "detected"),
        severity=row.get("severity", "warning"),
        confidence=float(row.get("confidence", 0.0) or 0.0),
        detected_at=row.get("detected_at", ""),
        event_revision=int(row.get("event_revision", 1) or 1),
        summary=row.get("summary", ""),
        recommended_actions=list(row.get("recommended_actions", []) or []),
        evidence_receipt=row.get("evidence_receipt", "") or "",
        investigation_id=row.get("investigation_id", "") or "",
        policy_version=row.get("policy_version", "") or "",
        history=list(row.get("history", []) or []),
    )


def report_alert(
    session: Session,
    *,
    tenant_id: str,
    cand: CandidateEvent,
    report: dict[str, Any],
    adapter: SaaSTestAdapter,
    gate: ReportPolicyGate | None = None,
) -> dict[str, Any]:
    """调查结论 → 策略门 → 正式事件 → outbox（全部落库）。返回结果摘要。

    幂等：同一 Episode（同一 investigation_id/component）反复调用得到**同一
    event_id**；首次为新建（detected→confirmed），其后为更新（revision 递增，
    状态不变，除非策略门判定需升级）。因此可安全地重放/重试。
    """
    gate = gate or ReportPolicyGate()
    event_type = report.get("event_type") or "unknown_event"
    device_ref = make_device_ref(cand.device_sn)
    episode_key = episode_key_from(str(report.get("investigation_id", "")),
                                   cand.component_ref)
    ikey = make_idempotency_key(device_ref, event_type, episode_key)
    event_id = make_event_id(device_ref, event_type, episode_key)

    existing = repo.get_alert(session, event_id, tenant_id=tenant_id)

    signals = build_signals(cand)
    # 已有活动 Episode → 告诉策略门"走更新/升级"，不新建（§14.3）
    if existing is not None and existing.get("state") in ACTIVE_STATES:
        signals["has_active_episode"] = True

    decision = gate.evaluate(report, signals)
    if not decision.report:
        return {"reported": False, "event_id": event_id,
                "decision": decision.to_dict(), "delivered": False}

    detected_at = str(report.get("detected_at", "") or "")

    if existing is None:
        # 新建：detected → confirmed（策略门通过）。history 由 AlertLifecycle 维护。
        event = AlertEvent(
            event_id=event_id, idempotency_key=ikey, device_ref=device_ref,
            component_ref=cand.component_ref, event_type=event_type,
            state="detected", severity=decision.severity,
            confidence=float(report.get("confidence", 0.0) or 0.0),
            detected_at=detected_at,
            summary=str(report.get("summary", "")),
            recommended_actions=list(report.get("recommended_actions", []) or []),
            investigation_id=str(report.get("investigation_id", "")),
            policy_version=decision.policy_version,
        )
        lc = AlertLifecycle(event)                 # 追加 created 历史
        lc.transition("confirmed", reason="策略门通过")
        revision = event.event_revision
    else:
        # 更新：沿用既有 event_id，不改状态，仅递增 revision 并追加审计历史
        event = _row_to_alert_event(existing)
        event.severity = decision.severity
        event.summary = str(report.get("summary", "")) or event.summary
        event.confidence = float(report.get("confidence", 0.0) or event.confidence)
        event.recommended_actions = list(
            report.get("recommended_actions", []) or event.recommended_actions)
        event.event_revision += 1
        event.history = list(event.history) + [{
            "from": event.state, "to": event.state,
            "reason": "策略门更新（同 Episode 再次调查）",
            "revision": event.event_revision,
        }]
        revision = event.event_revision

    repo.save_alert(session, event, tenant_id=tenant_id)

    # ---- 幂等投递（outbox 落库）----
    payload = _payload(event, decision)
    repo.upsert_outbox(session, tenant_id=tenant_id, idempotency_key=ikey,
                       revision=revision, payload=payload)
    ok, resp = adapter.deliver(payload, idempotency_key=ikey, revision=revision)
    delivered = False
    receipt = ""
    if ok:
        receipt = str(resp.get("receipt_id", ""))
        repo.mark_outbox_delivered(session, tenant_id=tenant_id,
                                   idempotency_key=ikey, revision=revision,
                                   receipt=receipt)
        delivered = True

    return {
        "reported": True, "event_id": event_id, "event_state": event.state,
        "event_revision": revision, "severity": event.severity,
        "delivered": delivered, "receipt": receipt,
        "decision": decision.to_dict(),
        "new": existing is None,
    }


def _payload(event: AlertEvent, decision: Any) -> dict[str, Any]:
    """投递载荷：对齐 §12.5（业务摘要 + 受控证据 Receipt，不含明文设备号）。"""
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
