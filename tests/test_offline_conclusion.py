"""`offline_demo._conclusion` 的结论措辞：**越线的是哪个量，得跟信号走**。

为什么单独一个文件
------------------
台账 A86：`_conclusion` 的三条分支一律写死"温度越过厂商保护阈值"，
于是电池候选的结论成了

    主机电池温度越过厂商保护阈值，且无变更干扰、证据完整，判为明确热故障。

电池报的是 SOC、母线电压、单体压差，**根本没有温度指标**——这句话错在
"指标名"，而且顺着往下错到底：根因假设写"热退化/冷却失效"，
处置建议写"检查冷却通道"。值班员照着它去查一个不存在的方向。

这条缺陷不是"哪个部件"的问题，是**结论模板假定所有异常都是热异常**。
所以测试不抽查几个部件，而是：`_signal_of` 能推出的每个信号族，
结论里都必须说那个族自己的话，且**不许出现温度**（除非族本身就是温度）。

配套的覆盖度检查在 `test_go2_sim_semantic.py::test_every_signal_has_a_conclusion_clause`
（表全不全）；这里管的是**句子里是否真的用上了**（表对不对）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from data_dog.data_plane.candidate import _signal_of
from data_dog.presentation import (
    _CONFIRMED_ACTIONS,
    _CONFIRMED_CAUSE,
    SIGNAL_CLAUSE,
)
from data_dog.providers.offline_demo import DeterministicEvidenceProvider

_ROOT = Path(__file__).resolve().parents[1]

# 温度类信号族：只有这些才允许在结论里出现"温度"。
_TEMPERATURE_FAMILIES = {"temperature", "temperature_high", "temperature_rising"}

# 无近期变更的部件（`RECENT_CHANGES` 里有 left_hip / battery.main / controller.main，
# 走 needs_human_review 分支；确认分支要用不在表里的部件才测得着）。
_CLEAN_COMPONENT = "joint_motor.front_left_knee"
_CHANGED_COMPONENT = "joint_motor.front_left_hip"


def _all_signals() -> set[str]:
    """复用语义层测试的提取器，拿到真链路里会出现的全部信号族。"""
    spec = importlib.util.spec_from_file_location(
        "_tgs", _ROOT / "tests/test_go2_sim_semantic.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {_signal_of(rid, fld) for rid, fld in mod._extract_rules()}


def test_battery_conclusion_never_mentions_temperature() -> None:
    """A86 的原始症状：电池候选被说成过温。"""
    for sig in ("soc", "bms_soc", "power_v", "cell_v_spread"):
        out = DeterministicEvidenceProvider._conclusion("battery.main", "critical", sig)
        summary = out["summary"]
        assert "温度" not in summary, (
            f"battery.main / {sig} 的结论里出现了温度：{summary!r}"
        )
        causes = [h["cause"] for h in out["hypotheses"]]
        assert not any("热" in c or "冷却" in c for c in causes), (
            f"battery.main / {sig} 的根因假设是热故障：{causes}"
        )
        assert not any("冷却" in a for a in out["recommended_actions"]), (
            f"battery.main / {sig} 建议查冷却通道：{out['recommended_actions']}"
        )


def test_battery_soc_conclusion_names_electricity() -> None:
    """措辞要具体：说"电量低于告警线"，不是含糊的"出现异常"。"""
    out = DeterministicEvidenceProvider._conclusion("battery.main", "critical", "bms_soc")
    assert out["summary"].startswith("主机电池")
    assert "电量" in out["summary"]
    assert "低于" in out["summary"], f"下限方向没表达出来：{out['summary']!r}"


def test_temperature_conclusion_still_says_temperature() -> None:
    """别把热故障那条改没了——温度族仍要说温度越上限。"""
    out = DeterministicEvidenceProvider._conclusion(
        _CLEAN_COMPONENT, "critical", "temperature_high")
    assert "温度" in out["summary"]
    assert "超过" in out["summary"] or "越过" in out["summary"]
    # 方向是越上限，不是破下限
    assert "低于" not in out["summary"], f"温度说成了下限方向：{out['summary']!r}"


def test_every_signal_family_only_mentions_its_own_metric() -> None:
    """全族遍历：非温度族不许提温度，且不能退回裸标识符。

    这条比抽查电池更狠——电池只是被发现的第一个受害者。
    """
    for sig in sorted(_all_signals()):
        for sev in ("critical", "warning"):
            out = DeterministicEvidenceProvider._conclusion(_CLEAN_COMPONENT, sev, sig)
            summary = out["summary"]
            if sig not in _TEMPERATURE_FAMILIES:
                assert "温度" not in summary, (
                    f"signal={sig!r} sev={sev} 的结论提到了温度：{summary!r}"
                )
            # 界面上不该出现下划线标识符（A81/A85 的老坑，这里顺带钉住）
            assert "_" not in summary, (
                f"signal={sig!r} 的结论漏出了标识符：{summary!r}"
            )


def test_changed_component_branch_also_follows_signal() -> None:
    """needs_human_review 分支原先也写死温度，同样要跟信号走。

    这条分支在有近期变更的部件上触发（left_hip 有固件升级记录）。
    """
    out = DeterministicEvidenceProvider._conclusion(
        _CHANGED_COMPONENT, "critical", "q")
    assert out["disposition"] == "needs_human_review"
    assert "温度" not in out["summary"], f"说成了热故障：{out['summary']!r}"
    assert "需人工复核" in out["summary"]


def test_clause_cause_action_tables_cover_the_same_families() -> None:
    """半句、根因、建议三张表必须同键集。

    A86 有**三个面**：结论说错指标、根因猜错机理、建议支错方向。
    只补其中一张，另外两张照样把值班员带偏——而且补了一句看起来就"改完了"，
    比不改更危险。这条测试就是钉住"要么三张一起有，要么都没有"。
    """
    clause = set(SIGNAL_CLAUSE)
    cause = set(_CONFIRMED_CAUSE)
    action = set(_CONFIRMED_ACTIONS)
    assert clause == cause, (
        f"有半句但没根因：{sorted(clause - cause)}；"
        f"有根因但没半句：{sorted(cause - clause)}"
    )
    assert clause == action, (
        f"有半句但没建议：{sorted(clause - action)}；"
        f"有建议但没半句：{sorted(action - clause)}"
    )

    print(f"PASS test_clause_cause_action_tables_cover_the_same_families ({len(clause)} 族)")


def test_unknown_signal_degrades_without_lying() -> None:
    """没登记的信号族：宁可含糊，也不猜方向。

    原实现的问题正是"猜"——不管什么信号都说温度越上限。
    """
    out = DeterministicEvidenceProvider._conclusion(
        _CLEAN_COMPONENT, "critical", "brand_new_signal")
    assert "温度" not in out["summary"]
    assert "出现异常" in out["summary"], out["summary"]
    # 空 signal 也不能炸，且同样不许提温度
    empty = DeterministicEvidenceProvider._conclusion(_CLEAN_COMPONENT, "critical", "")
    assert "温度" not in empty["summary"]

    print("PASS test_unknown_signal_degrades_without_lying")
