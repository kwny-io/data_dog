# -*- coding: utf-8 -*-
"""标准事件 Schema：对齐架构文档 12.3 CandidateEvent 和 12.4 InvestigationReport。

为了不额外引入依赖开销，这里用带类型注解的 dataclass + 简单的 to_dict，
未来如需严格校验可平滑迁移到 pydantic。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class EvidenceSummary:
    """一个信号在事件窗口内的变化摘要。"""
    key: str                        # 例如 temperature_c / current_a
    from_value: Any
    to_value: Any
    baseline: Optional[Any] = None  # 同工况基线（若有）
    unit: Optional[str] = None
    note: Optional[str] = None


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
    rule_matches: List[str] = field(default_factory=list)
    anomaly_score: float = 0.0
    evidence: List[EvidenceSummary] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    detector_provenance: str = ""   # operator_id:version

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Hypothesis:
    cause: str
    status: str                     # supported | unsupported | unverified
    evidence_refs: List[str] = field(default_factory=list)
    counter_evidence_refs: List[str] = field(default_factory=list)


@dataclass
class InvestigationReport:
    """Agent 复核后输出的结构化调查结论（对齐 12.4）。"""
    investigation_id: str
    disposition: str                # confirmed | rejected | inconclusive | needs_human_review
    summary: str = ""
    candidate_refs: List[str] = field(default_factory=list)
    event_type: Optional[str] = None
    severity: Optional[str] = None
    confidence: float = 0.0
    hypotheses: List[Hypothesis] = field(default_factory=list)
    counter_evidence_refs: List[str] = field(default_factory=list)
    missing_evidence: List[str] = field(default_factory=list)
    risk_window: Optional[str] = None
    recommended_actions: List[str] = field(default_factory=list)
    model_provenance: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d
