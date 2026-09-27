"""展示层结论派生（`presentation.derive_verdict`）与委托关系测试。

`derive_verdict` 是「一句话结论」的唯一实现：演示台（`scenarios.ScenarioRun`）
与 HTTP 调查端点都委托到它，故这里既测规则本身，也守住委托关系（防两边分叉）。
"""
from __future__ import annotations

from data_dog.presentation import component_cn, derive_verdict


def test_component_cn_maps_and_falls_back():
    assert component_cn("joint_motor.front_left_hip") == "左前髋关节电机"
    # 未知部件原样返回（不编造名字）
    assert component_cn("unknown.thing") == "unknown.thing"


def test_no_candidates_no_rejection():
    v, d = derive_verdict([], {}, {"joint_motor.front_left_hip":
                                   {"metrics": {"temperature_c": 61.0}}})
    assert "没有检测到异常" in v
    assert "规则范围" in d


def test_fail_closed_when_rejected():
    v, d = derive_verdict([], {"rejected": 1, "rejected_by_reason": {"resolve": 1}}, {})
    assert "拒绝" in v and "拒绝" in d


def test_no_metrics_explains_capability_boundary():
    v, d = derive_verdict([], {}, {"controller.main": {"metrics": {}}})
    assert "没有检测到异常" in v
    assert "对不上" in d


def test_overheat_names_temperature_and_threshold():
    cand = {"severity_hint": "critical", "component_ref": "joint_motor.front_left_hip",
            "rule_matches": ["MOTOR_TEMP_PROTECT"]}
    v, d = derive_verdict(
        [cand], {}, {"joint_motor.front_left_hip": {"metrics": {"temperature_c": 96.0}}})
    assert "严重" in v
    assert "96" in d and "90" in d


def test_count_reflects_number_of_candidates():
    cands = [{"severity_hint": "critical", "component_ref": "joint_motor.front_left_hip",
              "rule_matches": ["MOTOR_TEMP_PROTECT"]}] * 3
    v, _ = derive_verdict(cands, {}, {})
    assert "3 条" in v


def test_scenarios_delegates_to_presentation():
    """演示台结论必须与 derive_verdict 同源（守住委托，防前后端分叉）。"""
    from data_dog.data_plane.scenarios import run_all
    for run in run_all():
        expected_v, expected_d = derive_verdict(
            [c.to_dict() for c in run.candidates], run.stats,
            run.telemetry_by_component)
        assert run.verdict == expected_v
        assert run.verdict_detail == expected_d
