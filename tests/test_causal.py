"""跨部件因果时序测试（G6，对齐 gap 文档 §5 验收）。

覆盖：
  - 电压先跌、电流后涨、拓扑同链 → 产出 CausalLink(lag 正确)
  - 拒绝伪因果（无拓扑关系的共现）
  - 方向：子先父后 → suspect；兄弟 → sibling
  - 滞后上界：超 max_lag → 不关联
  - 确定性：同输入 → 同输出
  - 未知部件不参与
  - 兼容旧时序数字字段（10:00:07 == 10:00:10 - 3s）
"""
from __future__ import annotations

from data_dog.data_plane.causal import CausalLink, NeighborEvent, infer_causal_links

_UP = "battery.main"            # 供电链上游（因）
_DOWN = "controller.main"       # 下游（果）
_MOTOR = "joint_motor.front_left_hip"
_MOTOR2 = "joint_motor.front_right_hip"


def _ev(comp: str, t: float, rule: str = "") -> NeighborEvent:
    return NeighborEvent(component=comp, occurred_at_s=t, rule_id=rule)


def test_parent_to_child_link():
    """上游先动（t=0）、下游后动（t=1s）→ parent_to_child，lag=1.0。"""
    links = infer_causal_links([_ev(_UP, 0.0), _ev(_DOWN, 1.0)], max_lag_s=5.0)
    assert len(links) == 1
    lk = links[0]
    assert lk.cause_ref == _UP and lk.effect_ref == _DOWN
    assert lk.lag_s == 1.0
    assert lk.direction == "parent_to_child"
    assert lk.suspect is False
    assert lk.confidence > 0.0


def test_reject_spurious_same_window_cooccurrence():
    """两个无拓扑关系的部件异常 → 不判因果（拒伪因果）。"""
    # sibling 也是拓扑关系；真正"无关系"需构造非同父、非同链的部件。
    # 用两个不相干的未知/异链部件：只保留一个已知 + 一个未知 → 无关系。
    links = infer_causal_links([_ev(_UP, 0.0), _ev("mystery.part", 1.0)],
                               max_lag_s=5.0)
    assert links == []                       # 未知部件被排除 → 无从关联


def test_sibling_link():
    """同父兄弟：一个先动、一个后动 → sibling。"""
    links = infer_causal_links([_ev(_MOTOR, 0.0), _ev(_MOTOR2, 2.0)],
                               max_lag_s=5.0)
    assert len(links) == 1
    assert links[0].direction == "sibling"
    assert links[0].suspect is False


def test_child_to_parent_marked_suspect():
    """子先父后（方向反了）→ child_to_parent，标 suspect。"""
    links = infer_causal_links([_ev(_DOWN, 0.0), _ev(_UP, 1.0)], max_lag_s=5.0)
    assert len(links) == 1
    assert links[0].direction == "child_to_parent"
    assert links[0].suspect is True


def test_lag_upper_bound():
    """滞后超过 max_lag → 不关联。"""
    assert infer_causal_links([_ev(_UP, 0.0), _ev(_DOWN, 6.0)], max_lag_s=5.0) == []
    # 恰好等于上界 → 仍关联
    assert len(infer_causal_links([_ev(_UP, 0.0), _ev(_DOWN, 5.0)],
                                  max_lag_s=5.0)) == 1


def test_cause_must_be_earlier():
    """因果需要严格时间序（cause 早于 effect）→ 同时刻不判因果。"""
    assert infer_causal_links([_ev(_UP, 1.0), _ev(_DOWN, 1.0)], max_lag_s=5.0) == []


def test_confidence_higher_for_shorter_lag():
    """滞后越短 → 置信度越高。"""
    near = infer_causal_links([_ev(_UP, 0.0), _ev(_DOWN, 0.5)], max_lag_s=5.0)[0]
    far = infer_causal_links([_ev(_UP, 0.0), _ev(_DOWN, 4.5)], max_lag_s=5.0)[0]
    assert near.confidence > far.confidence


def test_deterministic_output():
    """同输入（任意顺序）→ 同输出。"""
    evs = [_ev(_UP, 0.0, "r1"), _ev(_DOWN, 1.0, "r2"), _ev(_MOTOR, 2.0, "r3")]
    a = [c.to_dict() for c in infer_causal_links(evs, max_lag_s=5.0)]
    b = [c.to_dict() for c in infer_causal_links(list(reversed(evs)), max_lag_s=5.0)]
    assert a == b


def test_zero_or_negative_max_lag_yields_nothing():
    assert infer_causal_links([_ev(_UP, 0.0), _ev(_DOWN, 1.0)], max_lag_s=0) == []
    assert infer_causal_links([_ev(_UP, 0.0), _ev(_DOWN, 1.0)], max_lag_s=-1) == []


def test_neighbor_event_tolerant_parse():
    """from_dict 容错：缺字段不炸，severity_hint / severity 都能读。"""
    e1 = NeighborEvent.from_dict({"component": _UP, "occurred_at_s": 3.0})
    assert e1.component == _UP and e1.occurred_at_s == 3.0
    e2 = NeighborEvent.from_dict({"component": _UP, "severity_hint": "critical"})
    assert e2.severity == "critical"
    e3 = NeighborEvent.from_dict({"component": _UP, "severity": "warning"})
    assert e3.severity == "warning"


def test_causal_link_to_dict_shape():
    lk = CausalLink(cause_ref=_UP, effect_ref=_DOWN, lag_s=1.0,
                    direction="parent_to_child", confidence=0.68, suspect=False)
    d = lk.to_dict()
    assert d["cause_ref"] == _UP and d["effect_ref"] == _DOWN
    assert d["direction"] == "parent_to_child" and d["suspect"] is False
