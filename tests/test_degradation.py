# -*- coding: utf-8 -*-
"""降级路径测试（对齐架构 §9.4）。

覆盖：
  - 硬规则候选（无论 Agent 是否可用）→ direct_report
  - Agent 不可用的普通候选 → queue_for_agent，且确实入队
  - Agent 可用的普通候选 → investigate_now
  - 队列容量上限 → 溢出的候选退化为 direct_report（不无限堆积）
  - 批量决策 / drain / reset 的语义
  - 判据函数 is_hard_rule 不与 ReportPolicyGate 冲突（可复用）
"""
from __future__ import annotations

from data_dog.agent.degradation import (
    DIRECT_REPORT,
    INVESTIGATE_NOW,
    QUEUE_FOR_AGENT,
    DegradationPolicy,
    is_hard_rule,
)
from data_dog.schemas.events import CandidateEvent


def _cand(cid="c1", component="joint_motor.front_left_hip",
          signal="temp_rising", sev="warning", rules=None):
    return CandidateEvent(
        candidate_id=cid, device_sn="device-abc123", component_ref=component,
        signal=signal, severity_hint=sev,
        window_start="09:55", window_end="10:00",
        sample_count=100, rule_matches=rules or [], anomaly_score=0.5,
        evidence=[], evidence_refs=[],
    )


# ---------- 1. 硬规则 → 直报（fail-safe） ----------

def test_hard_rule_direct_report_when_agent_down():
    crit = _cand("crit", "controller.main", "EMERGENCY_SHUTDOWN", sev="critical")
    d = DegradationPolicy(agent_available=False).decide(crit)
    assert d.path == DIRECT_REPORT
    assert d.candidate_id == "crit"


def test_hard_rule_direct_report_even_when_agent_up():
    """硬规则优先于可用性：Agent 在线也走直报，不等调查。"""
    crit = _cand("crit", "controller.main", "OVERTEMP", sev="critical")
    d = DegradationPolicy(agent_available=True).decide(crit)
    assert d.path == DIRECT_REPORT


def test_hard_rule_by_signal_hint_not_only_severity():
    """severity 是 warning，但信号里含严重错误码特征 → 仍算硬规则。"""
    c = _cand("c", "battery.main", "battery_OVERVOLT_alarm", sev="warning")
    d = DegradationPolicy(agent_available=True).decide(c)
    assert d.path == DIRECT_REPORT


# ---------- 2. Agent 不可用 → 排队 ----------

def test_queue_when_agent_down():
    policy = DegradationPolicy(agent_available=False)
    d = policy.decide(_cand("w"))
    assert d.path == QUEUE_FOR_AGENT
    assert policy.queued == ["w"]


def test_queue_is_bounded_then_falls_back_to_direct_report():
    """队列满后不再堆积：溢出候选退化为直报。"""
    policy = DegradationPolicy(agent_available=False, queue_capacity=2)
    assert policy.decide(_cand("a")).path == QUEUE_FOR_AGENT
    assert policy.decide(_cand("b")).path == QUEUE_FOR_AGENT
    d = policy.decide(_cand("c"))
    assert d.path == DIRECT_REPORT
    assert policy.queued == ["a", "b"]          # 队列未被撑大
    assert "队列已满" in d.reason


def test_drain_returns_and_clears():
    policy = DegradationPolicy(agent_available=False)
    policy.decide(_cand("a"))
    policy.decide(_cand("b"))
    assert policy.drain() == ["a", "b"]
    assert policy.queued == []
    policy.decide(_cand("c"))
    assert policy.queued == ["c"]


def test_reset_clears_queue():
    policy = DegradationPolicy(agent_available=False)
    policy.decide(_cand("a"))
    policy.reset()
    assert policy.queued == []


def test_recovery_replays_queued_after_agent_back():
    """Agent 恢复后：先把队列取回调查，新候选走 investigate_now。"""
    policy = DegradationPolicy(agent_available=False)
    policy.decide(_cand("a"))
    policy.agent_available = True
    assert policy.drain() == ["a"]
    assert policy.decide(_cand("b")).path == INVESTIGATE_NOW


# ---------- 3. Agent 可用 → 正常调查 ----------

def test_investigate_when_agent_up():
    assert DegradationPolicy().decide(_cand()).path == INVESTIGATE_NOW


# ---------- 4. 批量与判据 ----------

def test_decide_batch_preserves_order_and_ids():
    policy = DegradationPolicy(agent_available=False)
    cands = [_cand("a"), _cand("crit", sev="critical"), _cand("b")]
    decisions = policy.decide_batch(cands)
    assert [d.candidate_id for d in decisions] == ["a", "crit", "b"]
    assert [d.path for d in decisions] == [QUEUE_FOR_AGENT, DIRECT_REPORT, QUEUE_FOR_AGENT]


def test_is_hard_rule_edges():
    assert is_hard_rule("ok_signal", [], "critical") is True       # critical 兜底
    assert is_hard_rule("motor_STALL", [], "warning") is True       # 特征命中
    assert is_hard_rule("temp_rising", ["rule_x"], "warning") is False
    assert is_hard_rule("temp_rising", [], "") is False


def test_decision_serializes():
    d = DegradationPolicy().decide(_cand("x"))
    as_dict = d.to_dict()
    assert as_dict["candidate_id"] == "x"
    assert as_dict["path"] == INVESTIGATE_NOW
    assert set(as_dict) == {"candidate_id", "path", "reason", "severity_hint"}
