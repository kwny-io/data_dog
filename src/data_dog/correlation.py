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

from .data_plane.topology import TOPOLOGY
from .data_plane.topology import domain_of as _domain_of
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
# G6：此处不再硬编码——由 `data_plane/topology.py` 的 TOPOLOGY 派生（唯一真相源）。
# 保留 `COMPONENT_POWER_DOMAIN` 作为**兼容别名**（既有测试/调用点引用它）。
COMPONENT_POWER_DOMAIN: dict[str, str] = {
    ref: node.domain for ref, node in TOPOLOGY.items()
}


def component_power_domain(component: str) -> str:
    """部件 → 电源域；未知部件返回 `domain_<ref>` 避免与已知域误合并。"""
    return _domain_of(component)


def _window_start_s(s: str) -> float | None:
    """把窗口起点解析成可比较的秒数；无法解析 → None（退回精确字符串相等）。

    支持 ISO8601（含 Z）、"HH:MM"、"HH:MM:SS"。窗口格式多样，故**尽力解析、
    失败即退化为精确匹配**——保证既有形态（如纯时刻 "09:55"）零回归。
    """
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    parts = s.split(":")
    if len(parts) in (2, 3) and all(p.isdigit() for p in parts):
        h, m = int(parts[0]), int(parts[1])
        sec = int(parts[2]) if len(parts) == 3 else 0
        return h * 3600 + m * 60 + sec
    return None


def _duration_s(s: str) -> float:
    """把容差串（"00:05" 视作 HH:MM → 300s；也接受纯秒数）解析为秒；失败 → 0。"""
    parts = s.split(":")
    if len(parts) in (2, 3) and all(p.isdigit() for p in parts):
        h, m = int(parts[0]), int(parts[1])
        sec = int(parts[2]) if len(parts) == 3 else 0
        return h * 3600 + m * 60 + sec
    try:
        return float(s)
    except ValueError:
        return 0.0


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
    power_domain: str = ""            # A2 拓扑维度：所属电源域（来自 TOPOLOGY）
    linked_group_ids: list[str] = field(default_factory=list)  # 同域其他组
    # G6：拓扑关联带方向——{other_group_id: "upstream|downstream|sibling"}，谁先谁后。
    linked_group_roles: dict[str, str] = field(default_factory=dict)

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
            "linked_group_roles": self.linked_group_roles,
            "size": len(self.candidate_ids),
        }


def _window_delta_s(a: CorrelationGroup, b: CorrelationGroup) -> float | None:
    """两组窗口起点的秒差（b - a）；任一方无法解析 → None（调用方退回精确相等）。"""
    sa, sb = _window_start_s(a.window_start), _window_start_s(b.window_start)
    if sa is None or sb is None:
        return None
    return sb - sa


def _structural_role(self_ref: str, other_ref: str) -> str:
    """self 相对 other 的层级角色（用于给拓扑关联标方向）。

    - self 是 other 的祖先 → self 在上游（供电链上游，可能是因）；
    - other 是 self 的祖先 → self 在下游（可能是果）；
    - 同父 → 兄弟；其余 → unknown。
    """
    from .data_plane.topology import ancestors, parent_of
    if self_ref in ancestors(other_ref):
        return "upstream"
    if other_ref in ancestors(self_ref):
        return "downstream"
    pa = parent_of(self_ref)
    if pa is not None and pa == parent_of(other_ref):
        return "sibling"
    return "unknown"


class Correlator:
    """确定性关联器：把候选异常按结构化键归并成组。"""

    def __init__(self, window_tolerance: str = "00:05"):
        # G6：容差**已启用**（此前存而未用）。见 correlate 里的窗口重叠判定。
        self.window_tolerance = window_tolerance

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

        # 第二遍（A2 拓扑维度）：先给所有组标电源域（来自 TOPOLOGY 唯一真相源）
        for g in groups:
            representative = g.component_refs[0] if g.component_refs else ""
            g.power_domain = component_power_domain(representative)

        # 第三遍（G6）：同设备 + 同电源域 + **时间窗在容差内重叠** 的组互为拓扑关联，
        # 并标出方向（谁在上游/下游/兄弟）。容差从 window_tolerance 解析（"00:05"→300s）。
        tol_s = _duration_s(self.window_tolerance)
        for g in groups:
            linked: list[str] = []
            roles: dict[str, str] = {}
            for o in groups:
                if o is g or o.device_sn != g.device_sn:
                    continue
                if o.power_domain != g.power_domain:
                    continue
                if not _window_overlaps(g, o, tol_s):
                    continue
                linked.append(o.group_id)
                g_ref = g.component_refs[0] if g.component_refs else ""
                o_ref = o.component_refs[0] if o.component_refs else ""
                roles[o.group_id] = _structural_role(g_ref, o_ref)
            g.linked_group_ids = linked
            g.linked_group_roles = roles
        return groups


def _window_overlaps(a: CorrelationGroup, b: CorrelationGroup, tol_s: float) -> bool:
    """两组的窗口是否在容差内重叠。

    - 窗口起点可解析为秒 → 用 |Δ起点| ≤ 容差 判定（容忍相邻窗口的抖动）；
    - 无法解析（如纯时刻形态不一致）→ 退回**精确相等**，保持既有行为。
    """
    delta = _window_delta_s(a, b)
    if delta is None:
        return a.window_start == b.window_start and a.window_end == b.window_end
    return abs(delta) <= tol_s


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
