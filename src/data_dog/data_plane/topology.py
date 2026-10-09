"""部件层级拓扑的**唯一真相源**（G6）。

为什么需要
----------
部件的层级关系（电池 → 驱动板 → 电机）此前散落在多处硬编码
（`correlation.py` 的 `COMPONENT_POWER_DOMAIN`、`evidence/store.py` 的白名单、
各 fixtures），既没有统一结构，也无法表达"谁是谁的父"这种层级——
于是多事件关联只能做"同窗共现"，做不了"沿供电链传播"的因果。

本模块给出**唯一**的部件拓扑定义与几个纯函数（祖先 / 子 / 路径），其余模块
引用它，消除散落硬编码。

诚实边界
--------
- 拓扑表是**人工维护**的（对齐 A7 知识导入的做法）：真实车队部件拓扑可能更
  复杂/可变，这里给的是结构与接口，真实拓扑需按机型录入。
- 仅覆盖当前白名单内的 6 个部件（电池 / 主控 / 4 个电机）；未知 ref 不硬塞
  （返回空 / None），由调用方决定如何标注（如 `unconfirmed`）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ComponentNode:
    """拓扑里的一个部件节点。"""

    ref: str             # joint_motor.front_left_hip
    parent: str | None   # 层级父（供电链上游）；根部件为 None
    domain: str          # 所属总线域：power_bus / sensor_bus / ...
    kind: str            # 部件类型：motor / bms / board / sensor


# 唯一真相源。供电链：battery.main → controller.main → joint_motor.*
# （电池供电 → 主控驱动板 → 各关节电机；同属 power_bus）。
# 注意：当前白名单 6 个部件**确实同一供电域**（power_bus），故 domain 对它们
# 不产生区分；domain 的价值在引入其它总线（sensor_bus 等）后显现。
TOPOLOGY: dict[str, ComponentNode] = {
    "battery.main": ComponentNode(
        ref="battery.main", parent=None, domain="power_bus", kind="bms"),
    "controller.main": ComponentNode(
        ref="controller.main", parent="battery.main",
        domain="power_bus", kind="board"),
    "joint_motor.front_left_hip": ComponentNode(
        ref="joint_motor.front_left_hip", parent="controller.main",
        domain="power_bus", kind="motor"),
    "joint_motor.front_right_hip": ComponentNode(
        ref="joint_motor.front_right_hip", parent="controller.main",
        domain="power_bus", kind="motor"),
    "joint_motor.front_left_knee": ComponentNode(
        ref="joint_motor.front_left_knee", parent="controller.main",
        domain="power_bus", kind="motor"),
    "joint_motor.front_right_knee": ComponentNode(
        ref="joint_motor.front_right_knee", parent="controller.main",
        domain="power_bus", kind="motor"),
}


def is_known(ref: str) -> bool:
    """ref 是否在拓扑表中（未知部件不硬塞，由调用方标 unconfirmed）。"""
    return ref in TOPOLOGY


def domain_of(ref: str) -> str:
    """部件所属总线域；未知部件返回 `domain_<ref>`（避免与已知域误合并）。"""
    node = TOPOLOGY.get(ref)
    if node is not None:
        return node.domain
    return f"domain_{ref}"


def parent_of(ref: str) -> str | None:
    """直接层级父；根部件 / 未知返回 None。"""
    node = TOPOLOGY.get(ref)
    return node.parent if node is not None else None


def ancestors(ref: str) -> list[str]:
    """沿 parent 上溯的祖先链（近 → 远）。未知 / 无父 → []。

    例：`ancestors("joint_motor.front_left_hip")` → `["controller.main", "battery.main"]`
    （即供电链上游）。
    """
    out: list[str] = []
    seen = {ref}
    cur = TOPOLOGY.get(ref)
    while cur is not None and cur.parent is not None:
        p = cur.parent
        if p in seen:                       # 防御人工表误配出的环
            break
        seen.add(p)
        out.append(p)
        cur = TOPOLOGY.get(p)
    return out


def children(ref: str) -> list[str]:
    """直接子部件（parent == ref），按 ref 字典序保证确定。"""
    return sorted(r for r, n in TOPOLOGY.items() if n.parent == ref)


def propagate_path(a: str, b: str) -> list[str] | None:
    """从 a 到 b 的层级路径（含两端）；无层级关系（兄弟 / 无关 / 未知）→ None。

    - b 是 a 的祖先 → `[a, ..., b]`（沿链上溯）
    - a 是 b 的祖先 → `[a, ..., b]`（沿链下行）
    - 其余 → None
    """
    if not (is_known(a) and is_known(b)):
        return None
    if a == b:
        return [a]
    anc_a = ancestors(a)
    anc_b = ancestors(b)
    if b in anc_a:                          # b 是 a 的祖先
        return [a, *anc_a[:anc_a.index(b) + 1]]
    if a in anc_b:                          # a 是 b 的祖先
        return [a, *reversed(anc_b[:anc_b.index(a)]), b]
    return None


__all__ = [
    "ComponentNode",
    "TOPOLOGY",
    "ancestors",
    "children",
    "domain_of",
    "is_known",
    "parent_of",
    "propagate_path",
]
