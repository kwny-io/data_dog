# -*- coding: utf-8 -*-
"""P2 候选异常构建（对齐架构 §9.3 第 6 步 + §12.3）。

把 Detector 的命中合并、去重成**少量** CandidateEvent：
  - 同一 (device, component, signal) 在合并窗口内的多次命中 → 合成**一条**候选
    （不然刷屏；重复信号合并是 §9.3 第 6 步明确要求）。
  - 附上 evidence（指标 from/to/baseline）+ evidence_refs（受控引用）。
  - anomaly_score：由**命中严重度**映射（确定性，不用模型）。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..schemas.events import CandidateEvent, EvidenceSummary

_SEV_RANK = {"info": 0, "warning": 1, "critical": 2}
_SEV_SCORE = {"info": 0.3, "warning": 0.7, "critical": 1.0}


@dataclass
class _Group:
    device_sn: str
    component_ref: str
    signal: str
    window_start: str
    window_end: str
    rule_ids: List[str] = field(default_factory=list)
    severity: str = "info"
    sample_count: int = 0
    evidence: List[EvidenceSummary] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    operator_id: str = ""


class CandidateBuilder:
    """累积命中 → 合并成候选。"""

    def __init__(self, *, merge_window_s: float = 5.0):
        self.merge_window_s = merge_window_s
        self._groups: Dict[str, _Group] = {}

    @staticmethod
    def _key(device_sn: str, component_ref: str, signal: str) -> str:
        return f"{device_sn}|{component_ref}|{signal}"

    def add(self, *, device_sn: str, component_ref: str, metrics: Dict[str, Any],
            hits: List[Dict[str, Any]], occurred_at: str, received_at: str,
            raw_ref: str = "", baseline: Optional[Dict[str, float]] = None) -> None:
        """加入一个事件的所有命中（同一事件多规则命中 → 归到各自的 signal）。"""
        for h in hits:
            rule_id = h["rule_id"]
            signal = _signal_of(rule_id, h.get("field", ""))
            key = self._key(device_sn, component_ref, signal)
            g = self._groups.get(key)
            if g is None:
                g = _Group(device_sn=device_sn, component_ref=component_ref,
                           signal=signal, window_start=occurred_at, window_end=occurred_at)
                self._groups[key] = g
            g.window_end = occurred_at
            g.sample_count += 1
            if rule_id not in g.rule_ids:
                g.rule_ids.append(rule_id)
            # 严重度取最高
            sev = h.get("severity_hint", "info")
            if _SEV_RANK.get(sev, 0) > _SEV_RANK.get(g.severity, 0):
                g.severity = sev
            g.operator_id = h.get("operator_id", g.operator_id)
            # 证据（该字段的观测值 / 阈值 / 基线）
            fld = h.get("field", "")
            base = (baseline or {}).get(fld)
            g.evidence = [e for e in g.evidence if e.key != fld]   # 同字段只留最新
            g.evidence.append(EvidenceSummary(
                key=fld, from_value=base, to_value=h.get("value"),
                baseline=base, unit=None,
                note=f"{rule_id}: {h.get('operator')} {h.get('threshold')}",
            ))
            if raw_ref and raw_ref not in g.evidence_refs:
                g.evidence_refs.append(raw_ref)

    def build(self) -> List[CandidateEvent]:
        """产出候选列表（确定性排序：设备→部件→信号）。"""
        out: List[CandidateEvent] = []
        for key in sorted(self._groups):
            g = self._groups[key]
            cid = "cand_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
            out.append(CandidateEvent(
                candidate_id=cid,
                device_sn=g.device_sn,
                component_ref=g.component_ref,
                signal=g.signal,
                severity_hint=g.severity,
                window_start=g.window_start,
                window_end=g.window_end,
                sample_count=g.sample_count,
                rule_matches=list(g.rule_ids),
                anomaly_score=_SEV_SCORE.get(g.severity, 0.3),
                evidence=list(g.evidence),
                evidence_refs=list(g.evidence_refs) + [f"metricwin://{cid}"],
                detector_provenance=g.operator_id,
            ))
        return out


def _signal_of(rule_id: str, field_name: str) -> str:
    """规则的信号族：优先用 rule_id 语义，退化用字段名。"""
    r = rule_id.lower()
    if "overheat" in r or "protect" in r or (field_name == "temperature" and "temp" in r):
        return "temperature_high"
    if "temp" in r:
        return "temperature_rising"
    if "overcurrent" in r or "current" in r:
        return "current_anomaly"
    if "fault" in r or "mode" in r or "error" in r:
        return "fault_flag"
    return field_name or "unknown"
