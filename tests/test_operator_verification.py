"""P1 收尾测试：发布状态机 + Fuzz 健壮性 + 新旧差分。

覆盖：
  - 生命周期 happy path 到 Active
  - 非法转移被拒（确定性）
  - 门控拒绝：回放未全过 / 缺批准 / 缺签名 / 有状态缺 state_policy
  - Quarantine / Rollback 分支
  - Fuzz：畸形输入 0 崩溃（架构 §10.3）
  - 差分回放：无差异通过；未解释差异拒绝
"""
from __future__ import annotations

from pathlib import Path

import pytest

from data_dog.operator_factory.knowledge import build_knowledge_package
from data_dog.operator_factory.lifecycle import (
    OperatorLifecycle,
    TransitionError,
    run_happy_path,
)
from data_dog.operator_factory.p1_demo import build_motor_operator
from data_dog.operator_factory.pipeline import Fixture
from data_dog.operator_factory.verification import diff_replay, fuzz_operator

ROOT = Path(__file__).resolve().parents[1]
CLEANED = ROOT / "vendor_docs" / "unitree" / "cleaned"
FIXTURES = ROOT / "src" / "data_dog" / "operator_factory" / "fixtures"


@pytest.fixture(scope="module")
def op():
    return build_motor_operator(build_knowledge_package(CLEANED))


@pytest.fixture(scope="module")
def fixtures():
    return [Fixture.load(p) for p in sorted(FIXTURES.glob("*.json"))]


# ---------------------------------------------------------------- 生命周期

def test_lifecycle_happy_path_reaches_active():
    lc = run_happy_path("local.test", "1.0.0",
                        replay={"n": 5, "passed": 5}, signature="sig:abc")
    assert lc.state == "Active"
    # 历史每一步都成功
    assert all(h["ok"] for h in lc.history)


def test_lifecycle_rejects_illegal_transition():
    lc = OperatorLifecycle("op", "1.0")
    with pytest.raises(TransitionError, match="不能直接到"):
        lc.transition("Active")   # Draft 不能直接到 Active


def test_lifecycle_gate_rejects_failed_replay():
    lc = OperatorLifecycle("op", "1.0")
    lc.transition("Generated")
    lc.transition("Building", {"scan_ok": True})
    lc.transition("Validating", {"scan_ok": True})
    with pytest.raises(TransitionError, match="回放未全过"):
        lc.transition("AwaitingApproval",
                      {"replay": {"n": 5, "passed": 4},
                       "why_not_lower_layer": "x"})


def test_lifecycle_gate_requires_approval_and_signature():
    lc = OperatorLifecycle("op", "1.0")
    for t, ev in [("Generated", None), ("Building", {"scan_ok": True}),
                  ("Validating", {"scan_ok": True}),
                  ("AwaitingApproval", {"replay": {"n": 1, "passed": 1},
                                        "why_not_lower_layer": "x"})]:
        lc.transition(t, ev)
    with pytest.raises(TransitionError, match="人工批准"):
        lc.transition("Signed", {"approved": False})
    lc.transition("Signed", {"approved": True})
    with pytest.raises(TransitionError, match="签名"):
        lc.transition("Registered", {})


def test_lifecycle_stateful_requires_state_policy():
    lc = OperatorLifecycle("op", "1.0", has_state=True)
    for t, ev in [("Generated", None), ("Building", {"scan_ok": True}),
                  ("Validating", {"scan_ok": True}),
                  ("AwaitingApproval", {"replay": {"n": 1, "passed": 1},
                                        "why_not_lower_layer": "x"}),
                  ("Signed", {"approved": True})]:
        lc.transition(t, ev)
    with pytest.raises(TransitionError, match="state_policy"):
        lc.transition("Registered", {"signature": "sig:x"})   # 有状态却没声明策略


def test_lifecycle_quarantine_and_rollback():
    lc = run_happy_path("op", "1.0", replay={"n": 1, "passed": 1}, signature="s")
    lc.transition("Quarantined", {"reason": "crash rate 超限"})
    assert lc.state == "Quarantined"
    lc.transition("RolledBack", {"reason": "回退稳定版"})
    assert lc.state == "RolledBack"


# ---------------------------------------------------------------- Fuzz

def test_fuzz_no_crash(op):
    """畸形/随机输入不得让算子崩溃（§10.3）。这是 Fuzz 加固后的回归保证。"""
    res = fuzz_operator(op, iterations=500)
    assert res.crashes == 0, res.detail


def test_fuzz_deterministic(op):
    """同种子两次 Fuzz 结果一致（可复现）。"""
    a = fuzz_operator(op, iterations=100, seed=123)
    b = fuzz_operator(op, iterations=100, seed=123)
    assert (a.n, a.crashes) == (b.n, b.crashes)


def test_malformed_input_does_not_misfire(op):
    """恶意/畸形输入不得误命中关键规则（宁可漏报）。"""
    assert op.run({"temperature": "hot", "mode": "x"}) == []
    assert "MOTOR_TEMP_PROTECT" not in op.run({"temperature": None})
    assert op.run(None) == []


# ---------------------------------------------------------------- 新旧差分

def test_diff_same_operator_no_difference(op, fixtures):
    dr = diff_replay(op, op, fixtures)
    assert dr.ok
    assert all(not e.only_new and not e.only_old for e in dr.entries)


def test_diff_unexplained_difference_rejected(op, fixtures):
    """新版多了个"永远命中"的规则 → 差分必须判为不可接受（行为漂移）。"""
    import copy

    from data_dog.operator_factory.pipeline import DeclarativeRule
    new = copy.deepcopy(op)
    new.rules.append(DeclarativeRule(
        rule_id="ALWAYS_TRUE", field="temperature", operator="ge", value=-1e9,
        severity_hint="info", message="永远命中（模拟漂移）"))
    dr = diff_replay(op, new, fixtures)
    assert not dr.ok
    assert any(not e.accepted for e in dr.entries)
