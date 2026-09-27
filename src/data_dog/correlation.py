"""多事件确定性关联（对齐架构 14.1）—— 雏形。

架构要求：关联不能用文本相似度瞎猜，而应基于结构化的关联键。
这里实现一个轻量、确定性的 Correlator：
- 按 (device_sn, 时间窗重叠, 信号族) 把多个 Candidate 归并成一组。
- 同组的候选合并成一个「关联组」，供后续交给 Agent / 上报。

注意：这是**雏形**（部分实现）。真实系统还需要组件拓扑、电源域、因果时序、
同算子/固件发布的异常率变化等更多关联维度（见架构 14.1）。当前只覆盖
"同设备 + 同时间窗 + 同信号族"这一最基础的关联。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .schemas.events import CandidateEvent

# 信号族：把相近的异常信号归到一类（结构化映射，不是文本相似度）
SIGNAL_FAMILY: dict[str, str] = {
    "temperature_rising_under_normalized_load": "thermal",
    "temperature_spike": "thermal",
    "current_high": "electrical",
    "voltage_drop": "electrical",
    "load_spike": "mechanical",
    "vibration_high": "mechanical",
    "unknown_spike": "unknown",
}


def signal_family(signal: str) -> str:
    return SIGNAL_FAMILY.get(signal, "unknown")


# ---- A2 拓扑维度：部件所属电源域（对齐架构 14.1“同一电源域/总线”）----
COMPONENT_POWER_DOMAIN: dict[str, str] = {
    "battery.main": "power_bus",
    "controller.main": "power_bus",
    "joint_motor.front_left_hip": "power_bus",
    "joint_motor.front_right_hip": "power_bus",
    "joint_motor.front_left_knee": "power_bus",
}


def component_power_domain(component: str) -> str:
    """部件 → 电源域；未知部件返回 unknown_XXXX 避免与已知域误合并。"""
    dom = COMPONENT_POWER_DOMAIN.get(component)
    if dom:
        return dom
    return f"domain_{component}"


@dataclass
class CorrelationGroup:
    """一组被关联在一起的候选异常。"""
    group_id: str
    device_sn: str
    family: str
    window_start: str
    window_end: str
    candidate_ids: list[str] = field(default_factory=list)
    component_refs: list[str] = field(default_factory=list)
    power_domain: str = ""            # A2 拓扑维度：所属电源域
    linked_group_ids: list[str] = field(default_factory=list)  # 同域其他组

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "device_sn": self.device_sn,
            "family": self.family,
            "window": f"{self.window_start}-{self.window_end}",
            "candidate_ids": self.candidate_ids,
            "component_refs": self.component_refs,
            "power_domain": self.power_domain,
            "linked_group_ids": self.linked_group_ids,
            "size": len(self.candidate_ids),
        }


class Correlator:
    """确定性关联器：把候选异常按结构化键归并成组。"""

    def __init__(self, window_tolerance: str = "00:05"):
        self.window_tolerance = window_tolerance  # 雏形里暂存，未做复杂窗口重叠计算

    def correlate(self, candidates: list[CandidateEvent]) -> list[CorrelationGroup]:
        # 第一遍：按 设备 + 时间窗 + 信号族 归并基础组
        buckets: dict[tuple[str, str, str, str], CorrelationGroup] = {}
        for c in candidates:
            fam = signal_family(c.signal)
            key = (c.device_sn, fam, c.window_start, c.window_end)
            g = buckets.get(key)
            if g is None:
                g = CorrelationGroup(
                    group_id=f"grp_{len(buckets)+1}",
                    device_sn=c.device_sn,
                    family=fam,
                    window_start=c.window_start,
                    window_end=c.window_end,
                )
                buckets[key] = g
            g.candidate_ids.append(c.candidate_id)
            if c.component_ref not in g.component_refs:
                g.component_refs.append(c.component_ref)
        groups = list(buckets.values())

        # 第二遍（A2 拓扑维度）：先给所有组标电源域
        for g in groups:
            representative = g.component_refs[0] if g.component_refs else ""
            g.power_domain = component_power_domain(representative)

        # 第三遍：再算同设备+同窗+同电源域的其他组为"拓扑关联"
        # （必须在所有 power_domain 都设置完成后计算，避免顺序依赖）
        for g in groups:
            g.linked_group_ids = [
                o.group_id
                for o in groups
                if o is not g
                and o.device_sn == g.device_sn
                and o.window_start == g.window_start
                and o.window_end == g.window_end
                and o.power_domain == g.power_domain
            ]
        return groups


# ---- Agent 核心第 4 步：解释 / 合并（把一组关联报告合成一句可读的"事件说明"）----

def explain_episode(reports, group: dict) -> str:
    """把一个关联组里的若干份 InvestigationReport 合成一句解释性事件说明。

    纯确定性文本拼装（不调 LLM）：给出这一集"是什么、涉及哪些部件、汇总裁决"，
    供上层展示 / 讲稿使用。架构 6.2 里 Agent 的"解释"能力的最小落地。
    """
    comps = group.get("component_refs", [])
    fam = group.get("family", "unknown")
    disposition_counts: dict = {}
    for r in reports:
        disposition_counts[r.disposition] = disposition_counts.get(r.disposition, 0) + 1
    verdicts = "、".join(f"{k}×{v}" for k, v in disposition_counts.items()) or "无结论"
    return (
        f"该组发现 {len(reports)} 个相关候选，涉及部件 {_join_chs(comps)}，"
        f"信号族 {fam}；汇总裁决：{verdicts}。"
        "它们同设备、同窗口且结构相关，建议按同一故障事件一并处置。"
    )


def _join_chs(items) -> str:
    return "、" .join(items) if items else "未知"
