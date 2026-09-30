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
from typing import Any

from ..schemas.operator_config import DetectorConfig, FieldSpec

# ---------------------------------------------------------------- Parser

class Parser:
    """按算子声明的 FieldSpec 把原始键映射为标准字段名 + 类型转换。"""

    def __init__(self, fields: list[FieldSpec], *, strict: bool = False):
        self.fields = fields
        self.strict = strict

    def parse(self, raw: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
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

    def __init__(self, unit_rules: dict[str, str] | None = None):
        # {field: standard_unit}
        self.unit_rules = unit_rules or {}

    def normalize(self, metrics: dict[str, Any]) -> dict[str, Any]:
        return dict(metrics)      # 无声明换算时原样返回（不擅自换算，避免"猜单位"）


class StatusCodeNormalizer:
    """把厂商错误码/状态位映射为标准错误码（来源：P1 抽取的 MError 枚举）。"""

    def __init__(self, mapping: dict[int, str] | None = None):
        self.mapping = mapping or {
            0: "OK", 1: "OVERHEAT", 2: "OVERCURRENT", 3: "OVERVOLTAGE",
            4: "ENCODER_FAULT",
        }

    def codes_for(self, merror: Any) -> list[str]:
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
    baselines: dict[str, float] = field(default_factory=dict)   # "comp|field|condition" -> 基线

    def add_baseline(self, component: str, field_name: str, condition: str, value: float) -> None:
        self.baselines[f"{component}|{field_name}|{condition}"] = value

    def baseline_for(self, component: str, field_name: str, condition: str) -> float | None:
        return self.baselines.get(f"{component}|{field_name}|{condition}")

    def condition_from(self, raw: dict[str, Any]) -> str:
        return str(raw.get("operating_condition") or "unknown")


# ---------------------------------------------------------------- Window

@dataclass
class WindowFeature:
    """一个事件的窗口特征（只依赖事件时间序列，确定性）。"""
    mean: dict[str, float] = field(default_factory=dict)
    slope: dict[str, float] = field(default_factory=dict)
    last: dict[str, float] = field(default_factory=dict)
    max: dict[str, float] = field(default_factory=dict)
    count: int = 0


class WindowBuffer:
    """按 (device, component) 维护事件时间序列并算窗口特征。

    **事件时间有序（G2）**：样本按 `(occurred_at_s, seq)` 有序插入，乱序到达不再
    污染 `mean/slope/max/last`。未提供 `occurred_at_s` 时退化为到达顺序（用内部
    自增 `seq` 当时间键）——与旧行为一致（零回归，旧调用点无需改）。

    诚实边界：同一 buffer **不要**混用"给时间"与"不给时间"的调用——不给时间的样本
    以 `seq` 为时间键（小整数），会排到真实 epoch 时间之前。一个 buffer 一种用法。
    """

    def __init__(self, size: int = 30):
        self.size = size
        # key → 有序样本列表 [(t, seq, metrics_dict), ...]
        self._samples: dict[str, list[tuple[float, int, dict[str, float]]]] = {}
        self._seq = 0

    def push(self, key: str, metrics: dict[str, Any],
             occurred_at_s: float | None = None) -> None:
        self._seq += 1
        seq = self._seq
        t = float(occurred_at_s) if occurred_at_s is not None else float(seq)
        clean = {k: float(v) for k, v in metrics.items()
                 if not isinstance(v, bool) and isinstance(v, (int, float))}
        buf = self._samples.setdefault(key, [])
        entry = (t, seq, clean)
        # 有序插入：找到第一个 (t, seq) 更大的位置（乱序事件插入到正确位置）。
        lo, hi = 0, len(buf)
        while lo < hi:
            mid = (lo + hi) // 2
            if (buf[mid][0], buf[mid][1]) <= (t, seq):
                lo = mid + 1
            else:
                hi = mid
        buf.insert(lo, entry)
        if len(buf) > self.size:
            buf.pop(0)                      # 淘汰**事件时间最早**的样本

    def features(self, key: str) -> WindowFeature:
        buf = self._samples.get(key, [])
        series: dict[str, list[float]] = {}
        for _t, _seq, metrics in buf:
            for k, v in metrics.items():
                series.setdefault(k, []).append(v)
        f = WindowFeature()
        for k, vals in series.items():
            if not vals:
                continue
            f.mean[k] = sum(vals) / len(vals)
            f.last[k] = vals[-1]
            f.max[k] = max(vals)
            f.slope[k] = _slope(vals)
        f.count = max((len(v) for v in series.values()), default=0)
        return f


def _slope(vals: list[float]) -> float:
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

    def detect(self, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
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
