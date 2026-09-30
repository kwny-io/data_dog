"""跨部件**因果时序**关联（G6）。

共现不够
--------
邻居关联此前只做"同窗共现"（同一时间窗内别的部件也异常）。但"电压先跌 → 电流
后涨"里，**顺序与滞后**是有意义的：共现只说明"一起异常"，分不清因与果。

本模块把一组带时间的部件异常事件，判成**有方向、有滞后**的疑似因果链：

    因(a) 必须早于 果(b)，滞后 ≤ max_lag_s，且 a、b 在拓扑上有层级关系。

规则（确定性）
--------------
1. **时间序**：`cause.occurred_at < effect.occurred_at`，且 `0 < lag <= max_lag_s`；
2. **拓扑合理**：沿 `topology.py` 的 `TOPOLOGY`，`cause` 是 `effect` 的祖先
   （供电链正常传播 `parent_to_child`）或兄弟（`sibling`）；**无拓扑关系的纯时序
   共现不判因果**（否则会把无关部件的巧合当因果）；
3. **置信度**：拓扑越近 + 滞后越短 → 越高；反向（子先父后）→ `suspect=True`。

诚实边界
--------
- 这是**工程启发式**（时间序 + 拓扑相邻是强提示），**不是真正的因果推断**
  （那需干预实验或完整物理模型）。对外措辞一律用"疑似因果链"，不宣称确定因果。
- 滞后窗 `max_lag_s` 需按机型调（电气 / 热传播时标不同）；默认 5s。
- **单设备内**：本模块不跨设备（跨设备因果属更大工程）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .topology import ancestors, is_known, parent_of


@dataclass(frozen=True)
class NeighborEvent:
    """一个带事件时间的部件异常事件（因果推导的输入）。"""

    component: str
    occurred_at_s: float
    rule_id: str = ""
    severity: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any]) -> NeighborEvent:
        """从字典容错解析（缺 occurred_at_s 视为 0.0，由调用方保证已补时间）。"""
        return NeighborEvent(
            component=str(d.get("component", "")),
            occurred_at_s=float(d.get("occurred_at_s", 0.0) or 0.0),
            rule_id=str(d.get("rule_id", "")),
            severity=str(d.get("severity_hint", d.get("severity", ""))),
        )


@dataclass(frozen=True)
class CausalLink:
    """一条疑似因果链：cause 先异常，effect 随后（沿拓扑）。"""

    cause_ref: str
    effect_ref: str
    lag_s: float
    direction: str            # parent_to_child / sibling / child_to_parent
    confidence: float
    suspect: bool             # True = 方向反了（子先父后），仅作提示
    cause_rule: str = ""
    effect_rule: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "cause_ref": self.cause_ref,
            "effect_ref": self.effect_ref,
            "lag_s": self.lag_s,
            "direction": self.direction,
            "confidence": self.confidence,
            "suspect": self.suspect,
            "cause_rule": self.cause_rule,
            "effect_rule": self.effect_rule,
        }


# 拓扑关系 → 基础置信度（越接近供电链自然方向越高）
_BASE_CONF = {"parent_to_child": 0.85, "sibling": 0.55, "child_to_parent": 0.30}


def _relation(a: str, b: str) -> str | None:
    """a 相对 b 的拓扑方向：a 是 b 祖先 / 同父兄弟 / a 是 b 后代；无关 → None。"""
    if a == b:
        return None
    if a in ancestors(b):
        return "parent_to_child"          # a 上游、b 下游 → 正常传播方向
    if b in ancestors(a):
        return "child_to_parent"          # a 下游、b 上游 → 方向反了
    pa, pb = parent_of(a), parent_of(b)
    if pa is not None and pa == pb:
        return "sibling"
    return None


def infer_causal_links(
    events: list[NeighborEvent], *, max_lag_s: float = 5.0,
) -> list[CausalLink]:
    """找出"先发生 + 拓扑合理 + 滞后在窗内"的有序对。

    未知部件（不在拓扑表）不参与——避免硬塞。输出按 (cause, effect, lag) 排序，
    保证同输入 → 同输出（确定性）。
    """
    if max_lag_s <= 0:
        return []
    evs = [e for e in events if is_known(e.component)]
    out: list[CausalLink] = []
    for a in evs:
        for b in evs:
            if a is b or a.component == b.component:
                continue
            lag = b.occurred_at_s - a.occurred_at_s
            if lag <= 0.0 or lag > max_lag_s:
                continue                  # 时间序：因必须早于果且在滞后窗内
            rel = _relation(a.component, b.component)
            if rel is None:
                continue                  # 无拓扑关系 → 不判因果（拒伪因果）
            # 滞后越短置信度越高（线性衰减，下限 0.2×基础值）
            factor = max(0.2, 1.0 - lag / max_lag_s)
            conf = round(_BASE_CONF[rel] * factor, 3)
            out.append(CausalLink(
                cause_ref=a.component, effect_ref=b.component, lag_s=round(lag, 3),
                direction=rel, confidence=conf, suspect=(rel == "child_to_parent"),
                cause_rule=a.rule_id, effect_rule=b.rule_id,
            ))
    out.sort(key=lambda c: (c.cause_ref, c.effect_ref, c.lag_s))
    return out


__all__ = ["CausalLink", "NeighborEvent", "infer_causal_links"]
