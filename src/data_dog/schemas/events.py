"""标准事件 Schema：对齐架构文档 12.3 CandidateEvent 和 12.4 InvestigationReport。

为了不额外引入依赖开销，这里用带类型注解的 dataclass + 简单的 to_dict，
未来如需严格校验可平滑迁移到 pydantic。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class EvidenceSummary:
    """一个信号在事件窗口内的变化摘要。"""
    key: str                        # 例如 temperature_c / current_a
    from_value: Any
    to_value: Any
    baseline: Any | None = None  # 同工况基线（若有）
    unit: str | None = None
    note: str | None = None


@dataclass
class CandidateEvent:
    """检测算子输出、尚未完成 Agent 复核的候选异常（对齐 12.3）。"""
    candidate_id: str
    device_sn: str
    component_ref: str              # 例如 joint_motor.front_left_hip
    signal: str                     # 例如 temperature_rising_under_normalized_load
    severity_hint: str              # warning | critical
    window_start: str
    window_end: str
    sample_count: int
    rule_matches: list[str] = field(default_factory=list)
    anomaly_score: float = 0.0
    evidence: list[EvidenceSummary] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    detector_provenance: str = ""   # operator_id:version

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Hypothesis:
    cause: str
    status: str                     # supported | unsupported | unverified
    evidence_refs: list[str] = field(default_factory=list)
    counter_evidence_refs: list[str] = field(default_factory=list)


@dataclass
class InvestigationReport:
    """Agent 复核后输出的结构化调查结论（对齐 12.4）。"""
    investigation_id: str
    disposition: str                # confirmed | rejected | inconclusive | needs_human_review
    summary: str = ""
    candidate_refs: list[str] = field(default_factory=list)
    event_type: str | None = None
    severity: str | None = None
    confidence: float = 0.0
    hypotheses: list[Hypothesis] = field(default_factory=list)
    counter_evidence_refs: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    risk_window: str | None = None
    recommended_actions: list[str] = field(default_factory=list)
    model_provenance: dict[str, Any] = field(default_factory=dict)
    # ---- 多事件关联（A2/14.1）：单条报告可标记它属于哪个关联组、关联了哪些候选 ----
    correlation_group_id: str | None = None
    linked_candidate_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d
