"""P3 确定性上报策略门（对齐架构 §14.2）。

把 §14.2 的 OR 触发条件做成**可穷举、无歧义**的决策表：
  - 条件不满足 → 不上报（suppressed），给出原因；
  - 条件满足 → 上报，并给出**确定性**的 severity（冲突消解）；
  - 任何门控项缺失（证据不完整/版本过期）→ 不通过（失败关闭）。

§14.2 原文四种"至少满足其一"：
  ① 厂商明确严重错误码/安全规则命中
  ② 连续多个窗口异常且证据完整
  ③ Agent 复核 confirmed 且置信度与证据门槛达标
  ④ 多个中等级候选在关联窗口内共同满足组合规则

本模块把它们实现为 `REPORT_RULES`（有序、可判定），并补上原文要求但未给规则的：
  - severity 冲突消解：取**最高**严重度（critical > warning > info）；
  - 策略版本：决策记录 `policy_version`，便于审计与回归。

**为什么用决策表而不是让模型判断**：上报是副作用，必须可复现、可审计、可回归——
模型今天判 A 明天判 B 无法测试（对齐 §4.1 确定性热路径）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

POLICY_VERSION = "report-policy-v1"

_SEV_RANK = {"info": 0, "warning": 1, "critical": 2}
_SEV_ORDER = {0: "info", 1: "warning", 2: "critical"}

# 厂商明确严重错误码/安全规则（命中即可上报，对齐 §14.2 ①）
_VENDOR_SEVERE_CODES = {"OVERHEAT", "OVERCURRENT", "OVERVOLTAGE", "ENCODER_FAULT"}


@dataclass
class PolicyThresholds:
    """上报门槛（§24.2 建议默认值；可配置）。"""
    confirmed_min_confidence: float = 0.7      # 条件③：confirmed 的最低置信度
    multi_window_min: int = 2                  # 条件②：连续多个窗口
    multi_medium_min: int = 2                  # 条件④：多个中等级候选
    require_evidence: bool = True              # 门控：证据完整性


@dataclass
class PolicyDecision:
    report: bool
    severity: str = "info"
    reasons: list[str] = field(default_factory=list)
    policy_version: str = POLICY_VERSION
    suppressed_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "report": self.report, "severity": self.severity,
            "reasons": self.reasons, "policy_version": self.policy_version,
            "suppressed_reason": self.suppressed_reason,
        }


class ReportPolicyGate:
    """确定性上报门。evaluate(report, signals) → PolicyDecision。"""

    def __init__(self, thresholds: PolicyThresholds | None = None):
        self.t = thresholds or PolicyThresholds()

    def evaluate(self, report: dict[str, Any],
                 signals: dict[str, Any] | None = None) -> PolicyDecision:
        """report：InvestigationReport 的 dict；signals：来自候选/关联的补充信号。

        signals 可含：
          vendor_severe_codes: List[str]   命中的厂商严重错误码
          window_hits: int                 连续异常窗口数
          evidence_refs: List[str]         证据引用
          linked_candidates: int           关联到的候选数
          candidate_severities: List[str]  各候选的 severity_hint
        """
        s = signals or {}
        disposition = str(report.get("disposition", ""))
        confidence = float(report.get("confidence", 0.0) or 0.0)
        reasons: list[str] = []

        # ---- 门控（失败关闭）：证据完整性 ----
        evidence_refs = list(s.get("evidence_refs") or report.get("counter_evidence_refs") or [])
        # 证据主要看候选侧的 evidence_refs；由调用方放进 signals
        if self.t.require_evidence and not evidence_refs:
            return PolicyDecision(False, suppressed_reason="证据引用缺失（失败关闭）")

        # ---- 已存在活动事件 → 走"更新/升级"，不新建（§14.3）----
        if s.get("has_active_episode"):
            return PolicyDecision(
                True, severity=_resolve_severity(report, s),
                reasons=["同一 Episode 已存在活动事件 → 更新/升级而非新建"])

        # ---- 条件①：厂商明确严重错误码 / 安全规则 ----
        severe = set(s.get("vendor_severe_codes") or []) & _VENDOR_SEVERE_CODES
        if severe:
            reasons.append(f"条件①：厂商严重错误码命中 {sorted(severe)}")

        # ---- 条件②：连续多窗口异常且证据完整 ----
        if int(s.get("window_hits", 0)) >= self.t.multi_window_min:
            reasons.append(f"条件②：连续 {s.get('window_hits')} 个窗口异常（≥{self.t.multi_window_min}）")

        # ---- 条件③：Agent confirmed + 置信度/证据达标 ----
        if disposition == "confirmed" and confidence >= self.t.confirmed_min_confidence:
            reasons.append(f"条件③：confirmed 且置信度 {confidence:.2f} ≥ {self.t.confirmed_min_confidence}")
        elif disposition == "needs_human_review":
            # 需人工复核：不作为"可直接上报"，但可上报为"待人工确认"事件
            reasons.append("需人工复核：上报为待人工确认事件")

        # ---- 条件④：多个中等级候选共同满足 ----
        sevs = s.get("candidate_severities") or []
        medium = [x for x in sevs if x in ("warning", "critical")]
        if len(medium) >= self.t.multi_medium_min:
            reasons.append(f"条件④：{len(medium)} 个中/高等级候选在关联窗口内（≥{self.t.multi_medium_min}）")

        # ---- 排除类：rejected 不上报 ----
        if disposition == "rejected" and not reasons:
            return PolicyDecision(False, suppressed_reason="rejected（误报排除）")

        if not reasons:
            return PolicyDecision(False, suppressed_reason="未满足任何上报条件")

        return PolicyDecision(True, severity=_resolve_severity(report, s), reasons=reasons)


def _resolve_severity(report: dict[str, Any], signals: dict[str, Any]) -> str:
    """severity 冲突消解：取所有来源里的**最高**严重度。"""
    candidates = [str(report.get("severity") or "info")]
    candidates += [str(x) for x in (signals.get("candidate_severities") or [])]
    if signals.get("vendor_severe_codes"):
        candidates.append("critical")
    rank = max((_SEV_RANK.get(c, 0) for c in candidates), default=0)
    return _SEV_ORDER[rank]
