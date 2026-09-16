# -*- coding: utf-8 -*-
"""E2E 整链回放测试（对齐架构 §19.3 验收路径）。

验证：厂商文档 →[P1]→ 算子 →[P2]→ 候选 →[Agent]→ 报告 →[P3]→ SaaS 事件 整链贯通，
且各段产出的**设备/部件一致**（这是之前真实踩过的坑：provider 写死部件导致数据不对齐）。
"""
from __future__ import annotations

from data_dog.e2e.bridge import CandidateEvidenceBridge
from data_dog.e2e.replay import run_e2e


def test_e2e_chain_end_to_end():
    r = run_e2e()
    # P1 产出算子
    assert r.operator_id and r.operator_fields > 0
    # P2 产出候选（含失败关闭）
    assert r.ingested == 4
    assert r.rejected == 1                 # 固件不兼容那条
    assert len(r.candidates) >= 1
    # Agent 产出报告
    assert len(r.reports) == len(r.candidates)
    assert all(rep.disposition for rep in r.reports)
    # P3 至少上报一条
    assert any(d.report for d in r.decisions)
    assert r.alerts_delivered >= 1


def test_e2e_data_alignment():
    """整链设备/部件必须一致（否则 Agent 查不到证据 → 裁决退化）。"""
    r = run_e2e()
    cand = r.candidates[0]
    assert cand.device_sn == "device-go2-0001"
    assert cand.component_ref == "joint_motor.front_left_hip"
    # 高温候选应被 Agent 判为 confirmed（数据对齐后）
    assert r.reports[0].disposition == "confirmed"


def test_e2e_deterministic():
    """同一输入两次回放 → 相同裁决（可复现）。"""
    a = run_e2e()
    b = run_e2e()
    assert [x.disposition for x in a.reports] == [x.disposition for x in b.reports]
    assert a.alerts_delivered == b.alerts_delivered


def test_bridge_denies_cross_device():
    b = CandidateEvidenceBridge({"joint_motor.front_left_hip": {"x": 1}},
                                device_sn="device-go2-0001")
    res = b.query_telemetry("OTHER-device", "joint_motor.front_left_hip", "09:55", "10:00")
    assert res.get("ok") is False and res["error"] == "denied"


def test_bridge_denies_unknown_component():
    b = CandidateEvidenceBridge({}, device_sn="device-go2-0001")
    res = b.query_telemetry("device-go2-0001", "not_a_component", "09:55", "10:00")
    assert res.get("ok") is False and res["error"] == "denied"
