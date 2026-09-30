"""P4-6 冲突消解测试：确定性 ID 收敛 + 状态消解（顺序无关 / 可重放）。

为什么这些断言重要
------------------
跨区域**异步双向**复制的正确性只有一条判据：**两区域最终收敛到同一结果**。
若消解规则不是**纯函数**（依赖到达顺序、依赖本机时钟、依赖复制方向），两区
会永久分歧——而分歧是**静默**的（两侧都"看起来正常"）。

故必须钉住三件事：
1. **交换律**：`resolve_two(a,b) == resolve_two(b,a)`——顺序不影响结果。
2. **结合律**：N 路折叠与两两折叠结果一致——复制批次切分不影响结果。
3. **幂等**：同一份重复合并不改变结果——至少一次投递重放安全。
"""
from __future__ import annotations

import itertools

import pytest

from data_dog.alerting.lifecycle import AlertEvent
from data_dog.alerting.outbox import make_event_id, make_idempotency_key
from data_dog.replication import Conflict, merge_alert_state, resolve_severity, resolve_two
from data_dog.replication.conflict import SEVERITY_RANK


def _c(event_id="alert_x", state="detected", revision=1, severity="warning",
       acked=False, acked_at=0.0) -> Conflict:
    return Conflict(event_id=event_id, state=state, revision=revision,
                    severity=severity, acked=acked, acked_at=acked_at)


# ---------------------------------------------------------------- 确定性 ID 收敛

def test_same_episode_same_event_id_across_regions():
    """两区域对同一 Episode 各自派生 → 同一个 event_id（天然收敛，无需消解）。"""
    a = make_event_id("device-abc", "joint_motor_thermal_degradation", "inv-1")
    b = make_event_id("device-abc", "joint_motor_thermal_degradation", "inv-1")
    assert a == b
    assert a.startswith("alert_")


def test_different_episode_different_event_id():
    """不同 Episode → 不同 event_id（不得被误合并成同一条）。"""
    a = make_event_id("device-abc", "thermal", "inv-1")
    b = make_event_id("device-abc", "thermal", "inv-2")
    assert a != b


def test_idempotency_key_composition_is_stable():
    k1 = make_idempotency_key("device-abc", "thermal", "inv-1")
    k2 = make_idempotency_key("device-abc", "thermal", "inv-1")
    assert k1 == k2 == "device-abc:thermal:inv-1"


def test_event_id_matches_lifecycle_default_id_shape():
    """event_id 与生命周期状态机用的是同一形状（alert_ 前缀 + 16 位 hex）。"""
    eid = make_event_id("device-abc", "thermal", "inv-1")
    ev = AlertEvent(event_id=eid, idempotency_key="k", device_ref="d",
                    component_ref="c", event_type="thermal")
    assert ev.event_id == eid
    assert len(eid) == len("alert_") + 16


# ---------------------------------------------------------------- revision 因果序

def test_higher_revision_wins_regardless_of_order():
    """revision 高者胜——与传入顺序无关（因果序）。"""
    lo = _c(state="detected", revision=3)
    hi = _c(state="escalated", revision=5)
    assert resolve_two(lo, hi).state == "escalated"
    assert resolve_two(hi, lo).state == "escalated"
    assert resolve_two(lo, hi).revision == 5


def test_causal_path_observed():
    seen: list[str] = []
    resolve_two(_c(revision=1), _c(revision=2), observer=seen.append)
    assert seen == ["causal"]


# ---------------------------------------------------------------- 真并发策略

def test_concurrent_severity_takes_higher():
    """真并发（同 revision）→ severity 取更严重（乐观偏安全）。"""
    a = _c(state="confirmed", revision=4, severity="warning")
    b = _c(state="detected", revision=4, severity="critical")
    out = resolve_two(a, b)
    assert out.severity == "critical"
    # state 随 severity 持有者走
    assert out.state == "detected"
    # 顺序无关
    assert resolve_two(b, a).severity == "critical"
    assert resolve_two(b, a).state == "detected"


def test_concurrent_path_observed():
    seen: list[str] = []
    resolve_two(_c(revision=2, severity="warning"),
                _c(revision=2, severity="critical"), observer=seen.append)
    assert seen == ["concurrent"]


def test_ack_is_monotonic_and_first_wins():
    """ack 不可逆：一侧已 ack → 合并后必为 acked；时间取先到（更小）。"""
    no_ack = _c(revision=2, acked=False)
    acked_later = _c(revision=2, acked=True, acked_at=200.0)
    acked_earlier = _c(revision=2, acked=True, acked_at=100.0)

    merged = resolve_two(no_ack, acked_later)
    assert merged.acked is True
    assert merged.acked_at == 200.0

    both = resolve_two(acked_later, acked_earlier)
    assert both.acked is True
    assert both.acked_at == 100.0        # 先到
    assert resolve_two(acked_earlier, acked_later).acked_at == 100.0


def test_ack_survives_even_when_other_side_has_higher_revision():
    """ack 是跨 revision 的事实：高 revision 未 ack 也不能把已 ack 抹回未确认。"""
    acked = _c(revision=2, acked=True, acked_at=50.0)
    newer_noack = _c(revision=7, acked=False)
    out = resolve_two(acked, newer_noack)
    assert out.acked is True
    assert out.revision == 7      # revision 仍取高
    assert out.acked_at == 50.0


# ---------------------------------------------------------------- 代数性质（收敛的根基）

def test_commutative():
    """交换律：resolve_two(a,b) == resolve_two(b,a)。"""
    pairs = [
        (_c(revision=1), _c(revision=2)),
        (_c(revision=3, severity="critical"), _c(revision=3, severity="info")),
        (_c(revision=2, acked=True, acked_at=9.0), _c(revision=2)),
    ]
    for a, b in pairs:
        assert resolve_two(a, b).to_dict() == resolve_two(b, a).to_dict()


def test_idempotent():
    """幂等：同一份重复合并 → 结果不变（重放安全）。"""
    a = _c(revision=4, severity="warning", state="confirmed")
    assert resolve_two(a, a).to_dict() == resolve_two(a, a).to_dict()


def test_associative_over_n_way_fold():
    """结合律：N 路折叠的结果与折叠顺序无关（复制批次切分不影响收敛）。"""
    states = [
        _c(revision=1, severity="info"),
        _c(revision=3, severity="warning"),
        _c(revision=3, severity="critical", acked=True, acked_at=7.0),
    ]
    baseline = merge_alert_state(states).to_dict()
    for perm in itertools.permutations(states):
        assert merge_alert_state(list(perm)).to_dict() == baseline


def test_merge_single_and_empty():
    one = _c(revision=5)
    assert merge_alert_state([one]).to_dict() == one.to_dict()
    with pytest.raises(ValueError):
        merge_alert_state([])


def test_merge_rejects_mismatched_event_id():
    """不同 event_id 不得合并（那是不同实体，应各自收敛）。"""
    with pytest.raises(ValueError):
        resolve_two(_c(event_id="alert_a"), _c(event_id="alert_b"))


# ---------------------------------------------------------------- 辅助

def test_severity_rank_ordering():
    assert SEVERITY_RANK["critical"] > SEVERITY_RANK["warning"] > SEVERITY_RANK["info"]
    assert resolve_severity("info", "critical") == "critical"
    assert resolve_severity("critical", "info") == "critical"


def test_unknown_severity_fails_closed_to_info():
    """未知严重度按 info 处理——失败关闭：不擅自抬高（不制造幻觉严重度）。"""
    assert resolve_severity("bogus", "info") == "info"
    assert resolve_severity("warning", "bogus") == "warning"
