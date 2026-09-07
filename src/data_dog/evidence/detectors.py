# -*- coding: utf-8 -*-
"""用离线索生成的声明式算子做在线检测（桥接 A1 ← Agent 证据线）。

把 operators/generated/*.yaml 加载成规则检测器；query_telemetry 返回遥测时，
对当前部件套用其算子的 ThresholdRule，命中就记进结果，作为 Agent 能看到的证据。

字段名桥接：生成算子用 IDL 原始名（temperature/current/soc），内置遥测用
标准化名（temperature_c/current_a/state_of_charge）。统一归一后匹配。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parents[3]
OPERATORS_DIR = ROOT / "operators" / "generated"

# 规则字段名 → 遥测里可能的标准名（归一化查找）
_FIELD_ALIASES = {
    "temperature": ["temperature_c", "temperature", "cpu_temp_c"],
    "current": ["current_a", "current", "current"],
    "soc": ["state_of_charge", "soc"],
    "voltage": ["voltage_v", "voltage"],
}


def _lookup(telemetry: Dict[str, Any], rule_field: str) -> Optional[Any]:
    """在遥测里找规则字段的取值（含别名归一）。"""
    if rule_field in telemetry:
        return telemetry[rule_field]
    for alias in _FIELD_ALIASES.get(rule_field, []):
        if alias in telemetry:
            return telemetry[alias]
    return None


def _match(operator, actual, threshold):
    ops = {
        "gt": actual > threshold, "lt": actual < threshold,
        "ge": actual >= threshold, "le": actual <= threshold,
        "eq": actual == threshold,
    }
    return ops.get(operator, False)


class GeneratedRuleDetector:
    """从 operators/generated/ 加载声明式算子，提供按部件的检测。"""

    def __init__(self, operators_dir: Path = OPERATORS_DIR):
        self.by_component: Dict[str, dict] = {}
        self._load(operators_dir)

    def _load(self, d: Path) -> None:
        if not d.exists():
            return
        for f in sorted(d.glob("*.yaml")):
            try:
                cfg = yaml.safe_load(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            comp = cfg.get("component")
            if comp:
                self.by_component[comp] = cfg

    def detect(self, component: str, telemetry: Dict[str, Any]) -> List[Dict[str, Any]]:
        """对一份遥测应用该部件的阈值规则，返回命中列表（空 = 无异常）。"""
        cfg = self.by_component.get(component)
        if not cfg:
            return []
        hits = []
        for rule in cfg.get("rules", []):
            actual = _lookup(telemetry, rule["field"])
            if actual is None or not _match(rule["operator"], actual, rule["value"]):
                continue
            hits.append({
                "rule_id": rule["rule_id"],
                "field": rule["field"],
                "observed": actual,
                "operator": rule["operator"],
                "threshold": rule["value"],
                "severity_hint": rule.get("severity_hint", "warning"),
                "message": rule.get("message", ""),
                "operator_id": cfg.get("operator_id"),
            })
        return hits


def load_detector() -> GeneratedRuleDetector:
    return GeneratedRuleDetector()
