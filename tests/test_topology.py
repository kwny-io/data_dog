"""部件拓扑唯一真相源测试（G6，对齐 gap 文档 §5 验收）。

覆盖：
  - 祖先 / 子 / 路径解析
  - 未知部件不硬塞（is_known=False，domain_of 不误合并）
  - correlation 的 COMPONENT_POWER_DOMAIN 兼容别名与 TOPOLOGY 一致
  - ALLOWED_COMPONENTS 由 TOPOLOGY 派生（无漂移）
"""
from __future__ import annotations

from data_dog.correlation import COMPONENT_POWER_DOMAIN, component_power_domain
from data_dog.data_plane import topology
from data_dog.evidence.store import ALLOWED_COMPONENTS


def test_ancestors_power_chain():
    """沿 parent 上溯得到供电链。"""
    assert topology.ancestors("joint_motor.front_left_hip") == [
        "controller.main", "battery.main"]
    assert topology.ancestors("controller.main") == ["battery.main"]
    assert topology.ancestors("battery.main") == []       # 根


def test_children():
    assert "joint_motor.front_left_hip" in topology.children("controller.main")
    assert "controller.main" in topology.children("battery.main")
    assert topology.children("joint_motor.front_left_hip") == []


def test_propagate_path():
    # 下行：父 → 子
    assert topology.propagate_path("battery.main", "joint_motor.front_left_hip") == [
        "battery.main", "controller.main", "joint_motor.front_left_hip"]
    # 上溯：子 → 父
    assert topology.propagate_path("joint_motor.front_left_hip", "battery.main") == [
        "joint_motor.front_left_hip", "controller.main", "battery.main"]
    # 兄弟：无层级路径
    assert topology.propagate_path(
        "joint_motor.front_left_hip", "joint_motor.front_right_hip") is None
    # 未知部件
    assert topology.propagate_path("hacker.x", "battery.main") is None


def test_unknown_component_not_hardcoded():
    """未知部件：不硬塞进拓扑，域名为 domain_<ref>（避免与已知域误合并）。"""
    assert topology.is_known("mystery.part") is False
    assert topology.parent_of("mystery.part") is None
    assert topology.ancestors("mystery.part") == []
    assert topology.domain_of("mystery.part") == "domain_mystery.part"


def test_compat_alias_matches_topology():
    """correlation 的兼容别名与 TOPOLOGY 派生一致（迁移不漂移）。"""
    for ref, node in topology.TOPOLOGY.items():
        assert COMPONENT_POWER_DOMAIN[ref] == node.domain
        assert component_power_domain(ref) == node.domain


def test_allowed_components_derived_from_topology():
    """白名单由 TOPOLOGY 派生（消除散落硬编码）。"""
    assert ALLOWED_COMPONENTS == set(topology.TOPOLOGY.keys())


def test_domain_of_unknown_in_correlation():
    assert component_power_domain("mystery.part") == "domain_mystery.part"
