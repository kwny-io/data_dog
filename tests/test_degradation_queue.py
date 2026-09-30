"""降级队列持久化 + e2e 降级段测试（G4，对齐 gap 文档 §5 验收）。

覆盖：
  - 持久往返：StateStoreQueue push N → 新建 policy（模拟重启）→ drain 得同 N
  - 容量语义一致：满 → push False（退化直报），内存版与 state 版一致
  - 多副本共享：两个 policy 共享一个 StateStoreQueue → drain 不重复取
  - 队列项自描述：severity/attempts 随项持久
  - e2e 降级段：开启开关 → 回放含降级决策；关闭 → summary 不含该键（零回归）
"""
from __future__ import annotations

from data_dog.agent.degradation import (
    DIRECT_REPORT,
    INVESTIGATE_NOW,
    QUEUE_FOR_AGENT,
    DegradationPolicy,
)
from data_dog.agent.degradation_queue import (
    InMemoryQueue,
    QueueItem,
    StateStoreQueue,
    default_queue_backend,
    make_item,
)
from data_dog.e2e.replay import run_e2e
from data_dog.schemas.events import CandidateEvent
from data_dog.state.memory import InMemoryStateStore


def _cand(cid="c1", signal="temp_rising", sev="warning", rules=None):
    return CandidateEvent(
        candidate_id=cid, device_sn="device-abc123",
        component_ref="joint_motor.front_left_hip",
        signal=signal, severity_hint=sev,
        window_start="09:55", window_end="10:00",
        sample_count=100, rule_matches=rules or [], anomaly_score=0.5,
        evidence=[], evidence_refs=[],
    )


# ---------------------------------------------------------------- 队列项

def test_queue_item_roundtrip():
    it = make_item("c1", "warning", now=1234.0)
    d = it.to_dict()
    assert d == {"candidate_id": "c1", "severity": "warning",
                 "enqueued_at": 1234.0, "attempts": 0}
    assert QueueItem.from_dict(d) == it


def test_queue_item_tolerant_parse():
    """缺字段/脏值不崩（跨版本恢复）。"""
    it = QueueItem.from_dict({"candidate_id": "x"})
    assert it.candidate_id == "x" and it.severity == "" and it.attempts == 0


# ---------------------------------------------------------------- 内存后端

def test_inmemory_matches_old_semantics():
    q = InMemoryQueue()
    assert q.push(make_item("a"), capacity=2) is True
    assert q.push(make_item("b"), capacity=2) is True
    assert q.push(make_item("c"), capacity=2) is False       # 满
    assert [i.candidate_id for i in q.items()] == ["a", "b"]
    assert [i.candidate_id for i in q.drain()] == ["a", "b"]
    assert len(q) == 0


# ---------------------------------------------------------------- 持久后端

def test_statestore_queue_persists_across_policy_restart():
    """push N → 新建 policy（模拟重启）→ drain 得同 N 项。"""
    store = InMemoryStateStore()
    q = StateStoreQueue(store)
    p1 = DegradationPolicy(agent_available=False, queue_capacity=10, queue=q)
    for cid in ("a", "b", "c"):
        assert p1.decide(_cand(cid)).path == QUEUE_FOR_AGENT

    # 模拟重启：同一共享状态，全新 policy（新后端实例读同一 key）。
    p2 = DegradationPolicy(agent_available=False, queue_capacity=10,
                           queue=StateStoreQueue(store))
    assert p2.queued == ["a", "b", "c"]
    assert p2.drain() == ["a", "b", "c"]


def test_statestore_queue_capacity_semantics_consistent():
    store = InMemoryStateStore()
    p = DegradationPolicy(agent_available=False, queue_capacity=2,
                          queue=StateStoreQueue(store))
    assert p.decide(_cand("a")).path == QUEUE_FOR_AGENT
    assert p.decide(_cand("b")).path == QUEUE_FOR_AGENT
    d = p.decide(_cand("c"))
    assert d.path == DIRECT_REPORT and "队列已满" in d.reason
    assert p.queued == ["a", "b"]


def test_statestore_queue_shared_between_replicas_no_double_drain():
    """两个副本共享一个队列 → drain 不重复取。"""
    store = InMemoryStateStore()
    shared = StateStoreQueue(store)
    r1 = DegradationPolicy(agent_available=False, queue_capacity=10, queue=shared)
    r2 = DegradationPolicy(agent_available=False, queue_capacity=10, queue=shared)
    r1.decide(_cand("a"))
    r2.decide(_cand("b"))
    # r1 drain 取走全部 → r2 再也取不到（共享）
    assert r1.drain() == ["a", "b"]
    assert r2.drain() == []


def test_statestore_queue_persists_item_metadata():
    store = InMemoryStateStore()
    q = StateStoreQueue(store)
    q.push(make_item("a", "critical"), capacity=10)
    items = q.items()
    assert items[0].severity == "critical"
    assert items[0].enqueued_at > 0


def test_statestore_queue_reset():
    store = InMemoryStateStore()
    q = StateStoreQueue(store)
    q.push(make_item("a"), capacity=10)
    q.reset()
    assert len(q) == 0 and q.items() == []


def test_statestore_queue_tolerates_corrupt_state():
    """底层状态被写成非 list（脏数据）→ 当作空队列，不崩。"""
    store = InMemoryStateStore()
    store.mutate("degradation:queue", lambda _c: ({"not": "a list"}, None))
    q = StateStoreQueue(store)
    assert len(q) == 0
    assert q.push(make_item("a"), capacity=5) is True
    assert q.drain()[0].candidate_id == "a"


# ---------------------------------------------------------------- 后端选择

def test_default_backend_follows_setting(monkeypatch):
    monkeypatch.delenv("DD_DEGRADATION_QUEUE", raising=False)
    assert isinstance(default_queue_backend(), InMemoryQueue)
    monkeypatch.setenv("DD_DEGRADATION_QUEUE", "state")
    assert isinstance(default_queue_backend(), StateStoreQueue)


# ---------------------------------------------------------------- e2e 降级段

def test_e2e_without_degradation_is_zero_regression():
    """默认关：summary 不含 degradation 键（与既有回放逐字节一致）。"""
    r = run_e2e()
    assert "degradation" not in r.summary()
    assert r.degradation == []


def test_e2e_with_degradation_segment():
    r = run_e2e(with_degradation=True)
    s = r.summary()
    assert "degradation" in s
    # 候选里至少一条高温命中硬规则 → 直报；其余在 agent 不可用下排队/溢出。
    paths = s["degradation"]["paths"]
    assert len(paths) == len(r.candidates)
    assert set(paths) <= {DIRECT_REPORT, QUEUE_FOR_AGENT, INVESTIGATE_NOW}
    assert DIRECT_REPORT in paths
    # 恢复后 drain 取回排队候选
    assert set(r.degradation_drained).issubset({d["candidate_id"]
                                                for d in r.degradation})
