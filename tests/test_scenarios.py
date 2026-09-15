# -*- coding: utf-8 -*-
"""演示场景回归测试：锁定"候选由 P2 真实产出、且与证据同源"。

为什么需要它
------------
早先 UI 的候选是手写的（`rule_matches=["TEST"]`），既讲不清来源，还可能
"候选说 A、证据说 B"。本测试把这条链锁死：

    原始日志 → P2 数据面 → 候选 ──┐
                    └→ 归一化遥测 → Agent 证据（同一批日志，必须一致）

并锁住三个演示结论：过温→critical、正常→无候选、固件不符→失败关闭。
"""
from __future__ import annotations

import pytest

from data_dog.agent.loop import AgentLoop
from data_dog.data_plane.scenarios import (
    DEMO_DEVICE_SN, SCENARIOS, match_scenarios, run_all,
)
from data_dog.e2e.bridge import CandidateEvidenceBridge
from data_dog.evidence.store import ALLOWED_COMPONENTS
from data_dog.providers.offline_demo import DeterministicEvidenceProvider
from data_dog.tools.investigation import InvestigationTools


# ---------------------------------------------------------------- 问题检索

def test_empty_query_returns_all():
    assert len(match_scenarios("")) == len(SCENARIOS)
    assert len(match_scenarios("   ")) == len(SCENARIOS)


def test_keyword_search_finds_relevant_scenarios():
    assert "fail-closed-firmware" in {s.key for s in match_scenarios("固件")}
    assert "current-no-rule" in {s.key for s in match_scenarios("电流")}
    keys = {s.key for s in match_scenarios("温度")}
    assert {"overheat-with-change", "normal-no-alarm"} <= keys


def test_unknown_keyword_returns_nothing():
    assert match_scenarios("这个词肯定没有") == []


def test_every_scenario_has_keywords():
    """没有关键词的场景在演示台里检索不到，等于不可达。"""
    for s in SCENARIOS:
        assert s.keywords, s.key


# ---------------------------------------------------------------- 界面文案

def test_verdicts_exist_and_are_plain(runs):
    for r in runs:
        assert r.short_name and r.verdict and r.verdict_detail
        # 结论里不该出现内部设备号
        assert DEMO_DEVICE_SN not in r.verdict_detail


def test_overheat_verdict_names_temperature_and_threshold(runs):
    d = _run(runs, "overheat-no-change").verdict_detail
    assert "95" in d and "90" in d          # 实测温度 + 厂商阈值


def test_fail_closed_verdict_says_rejected(runs):
    d = _run(runs, "fail-closed-firmware").verdict_detail
    assert "拒绝" in d


@pytest.fixture(scope="module")
def runs():
    return run_all()


def _run(runs, key):
    return next(r for r in runs if r.scenario.key == key)


# ---------------------------------------------------------------- 数据面行为

def test_every_scenario_runs_deterministically(runs):
    """跑两次结果必须完全一致（确定性是这套系统的底线）。"""
    again = run_all()
    assert [len(r.candidates) for r in runs] == [len(r.candidates) for r in again]
    assert [r.stats for r in runs] == [r.stats for r in again]


def test_overheat_scenarios_produce_critical_candidates(runs):
    for key in ("overheat-with-change", "overheat-no-change"):
        r = _run(runs, key)
        assert len(r.candidates) == 1, key
        c = r.candidates[0]
        assert c.severity_hint == "critical"
        assert "MOTOR_TEMP_PROTECT" in c.rule_matches


def test_normal_and_borderline_produce_no_candidate(runs):
    """正常数据/未越线 → 不产出候选（这是"不误报"，不是故障）。"""
    for key in ("normal-no-alarm", "borderline-no-candidate"):
        r = _run(runs, key)
        assert r.candidates == [], key
        assert r.stats["rejected"] == 0


def test_firmware_mismatch_fails_closed(runs):
    r = _run(runs, "fail-closed-firmware")
    assert r.candidates == []
    assert r.stats["rejected"] == 1
    assert r.stats["rejected_by_reason"] == {"resolve": 1}


def test_every_candidate_produces_aligned_evidence(runs):
    """核心不变量：每个候选都在同一次运行里留下了对齐的证据。"""
    for r in runs:
        for c in r.candidates:
            tel = r.telemetry_by_component.get(c.component_ref)
            assert tel is not None, f"{c.component_ref} 无证据"
            assert tel["metrics"], "证据指标为空"
            assert tel["device_sn"] == DEMO_DEVICE_SN


# ---------------------------------------------------------------- 安全问题

def test_all_scenario_components_are_within_whitelist(runs):
    for r in runs:
        for c in r.candidates:
            assert c.component_ref in ALLOWED_COMPONENTS


def test_candidate_device_matches_trusted_context(runs):
    """候选设备必须等于可信上下文设备，否则工具层会失败关闭。"""
    from data_dog.context import default_context
    for r in runs:
        for c in r.candidates:
            assert c.device_sn == DEMO_DEVICE_SN == default_context().device_sn


# ---------------------------------------------------------------- 完整演示链

def _investigate(r):
    """跑一条候选的完整调查（离线确定性 Provider）。"""
    c = r.candidates[0]
    bridge = CandidateEvidenceBridge(r.telemetry_by_component, device_sn=DEMO_DEVICE_SN)
    agent = AgentLoop(DeterministicEvidenceProvider(),
                      InvestigationTools(bridge))
    return agent.investigate(c)


def test_overheat_with_recent_change_needs_human_review(runs):
    """有固件变更干扰 → 不能直接下结论，转人工复核。"""
    rep = _investigate(_run(runs, "overheat-with-change"))
    assert rep.disposition == "needs_human_review"
    assert rep.missing_evidence          # 必须承认缺证据


def test_overheat_without_change_is_confirmed(runs):
    """证据干净 → 明确热故障。"""
    rep = _investigate(_run(runs, "overheat-no-change"))
    assert rep.disposition == "confirmed"
    assert rep.confidence >= 0.7


def test_agent_sees_the_same_temperature_as_the_candidate(runs):
    """候选与证据必须同源：这是本模块存在的理由。"""
    r = _run(runs, "overheat-no-change")
    cand = r.candidates[0]
    tel = r.telemetry_by_component[cand.component_ref]
    assert tel["metrics"]["temperature_c"] == 95.0     # 与原始日志一致
    rep = _investigate(r)
    assert rep.disposition == "confirmed"
    assert "温度" in (rep.summary or "")
