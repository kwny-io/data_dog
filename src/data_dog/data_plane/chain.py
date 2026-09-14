# -*- coding: utf-8 -*-
"""P2 在线处理链（对齐架构 §9.3）。

    Parser → Normalizer → Enricher → Window/Feature → Detector → (候选)

- Parser：把厂商私有格式/字段按声明式映射解成标准字段（**执行 P1 的 FieldSpec 映射**）。
- Normalizer：单位/枚举/错误码规范化。
- Enricher：补设备元数据、组件拓扑、运行工况（**没工况就没基线**）。
- Window：滑动窗口特征（均值/斜率/分位数/极值）——热退化是趋势不是瞬时值。
- Detector：执行 P1 生成的算子（声明式 ThresholdRule + 受限 DSL），产出命中。

全程**确定性、不调大模型**（§4.1）；字段缺失走 missing 语义，不中断数据流（§10.3）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..schemas.operator_config import DetectorConfig, FieldSpec


# ---------------------------------------------------------------- Parser

class Parser:
    """按算子声明的 FieldSpec 把原始键映射为标准字段名 + 类型转换。"""

    def __init__(self, fields: List[FieldSpec], *, strict: bool = False):
        self.fields = fields
        self.strict = strict

    def parse(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for spec in self.fields:
            if spec.source not in raw:
                if not spec.optional:
                    out.setdefault("__missing__", []).append(spec.source)
                continue
            out[spec.name] = _coerce(raw[spec.source], spec.type)
        return out


def _coerce(value: Any, typ: str) -> Any:
    try:
        if typ in ("uint8", "uint16", "uint32", "int8", "int16", "int32"):
            return int(value)
        if typ in ("float32", "float64", "float"):
            return float(value)
        if typ in ("bool", "boolean"):
            return bool(value)
    except (TypeError, ValueError):
        return value          # 转换失败保留原值，交由下游 missing 语义处理
    return value


# ---------------------------------------------------------------- Normalizer

# 单位换算表（文档声明的量纲 → 标准量纲）
_UNIT_CONV = {
    ("celsius", "celsius"): lambda x: x,
    ("raw_temp", "celsius"): lambda x: x,               # 由算子声明具体换算
}


class Normalizer:
    """单位/量纲规范化。声明式 ->标准化声明（本层只做**有来源**的换算，不猜）。"""

    def __init__(self, unit_rules: Optional[Dict[str, str]] = None):
        # {field: standard_unit}
        self.unit_rules = unit_rules or {}

    def normalize(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        return dict(metrics)      # 无声明换算时原样返回（不擅自换算，避免"猜单位"）


class StatusCodeNormalizer:
    """把厂商错误码/状态位映射为标准错误码（来源：P1 抽取的 MError 枚举）。"""

    def __init__(self, mapping: Optional[Dict[int, str]] = None):
        self.mapping = mapping or {
            0: "OK", 1: "OVERHEAT", 2: "OVERCURRENT", 3: "OVERVOLTAGE",
            4: "ENCODER_FAULT",
        }

    def codes_for(self, merror: Any) -> List[str]:
        try:
            m = int(merror)
        except (TypeError, ValueError):
            return []
        code = self.mapping.get(m)
        return [code] if code and code != "OK" else []


# ---------------------------------------------------------------- Enricher

@dataclass
class Enricher:
    """补运行工况与同工况基线。基线按 (component, condition) 维护。"""
    baselines: Dict[str, float] = field(default_factory=dict)   # "comp|field|condition" -> 基线

    def add_baseline(self, component: str, field_name: str, condition: str, value: float) -> None:
        self.baselines[f"{component}|{field_name}|{condition}"] = value

    def baseline_for(self, component: str, field_name: str, condition: str) -> Optional[float]:
        return self.baselines.get(f"{component}|{field_name}|{condition}")

    def condition_from(self, raw: Dict[str, Any]) -> str:
        return str(raw.get("operating_condition") or "unknown")


# ---------------------------------------------------------------- Window

@dataclass
class WindowFeature:
    """一个事件的窗口特征（只依赖事件时间序列，确定性）。"""
    mean: Dict[str, float] = field(default_factory=dict)
    slope: Dict[str, float] = field(default_factory=dict)
    last: Dict[str, float] = field(default_factory=dict)
    max: Dict[str, float] = field(default_factory=dict)
    count: int = 0


class WindowBuffer:
    """按 (device, component) 维护事件时间序列并算窗口特征。"""

    def __init__(self, size: int = 30):
        self.size = size
        self._series: Dict[str, Dict[str, List[float]]] = {}

    def push(self, key: str, metrics: Dict[str, Any]) -> None:
        s = self._series.setdefault(key, {})
        for k, v in metrics.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            s.setdefault(k, []).append(float(v))
            if len(s[k]) > self.size:
                s[k].pop(0)

    def features(self, key: str) -> WindowFeature:
        s = self._series.get(key, {})
        f = WindowFeature()
        for k, vals in s.items():
            if not vals:
                continue
            f.mean[k] = sum(vals) / len(vals)
            f.last[k] = vals[-1]
            f.max[k] = max(vals)
            f.slope[k] = _slope(vals)
        f.count = max((len(v) for v in s.values()), default=0)
        return f


def _slope(vals: List[float]) -> float:
    n = len(vals)
    if n < 2:
        return 0.0
    mx = (n - 1) / 2
    my = sum(vals) / n
    num = sum((i - mx) * (v - my) for i, v in enumerate(vals))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


# ---------------------------------------------------------------- Detector

class Detector:
    """执行声明式算子（ThresholdRule）。DSL 规则由 operator_factory 的 Evaluator 执行。"""

    def __init__(self, config: DetectorConfig):
        self.config = config

    def detect(self, metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
        hits: List[Dict[str, Any]] = []
        for rule in self.config.rules:
            if rule.field not in metrics:
                continue
            val = metrics[rule.field]
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue                    # 畸形输入不命中（§10.3）
            if _cmp(rule.operator, val, rule.value):
                hits.append({
                    "rule_id": rule.rule_id,
                    "field": rule.field,
                    "value": val,
                    "operator": rule.operator,
                    "threshold": rule.value,
                    "severity_hint": rule.severity_hint,
                    "message": rule.message,
                    "operator_id": self.config.operator_id,
                })
        return hits


def _cmp(op: str, a: float, b: float) -> bool:
    return {
        "gt": a > b, "lt": a < b, "ge": a >= b, "le": a <= b, "eq": a == b,
    }.get(op, False)
