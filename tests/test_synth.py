# -*- coding: utf-8 -*-
"""Go2 合成遥测生成器 + ground truth 评估的测试。

验证：
- 合成样本物理自洽：正常样本温度/电流接近基线；故障样本有可区分的抬升。
- 5 类故障都被注入，且每份样本带 ground truth 标签（部件+窗口+故障类型）。
- 用合成数据跑 Agent 调查（确定性 provider），对比 ground truth，正常→rejected、
  故障→needs_human_review/confirmed，能得到高准确率。
"""
import json

import numpy as np

from data_dog.synth import (
    SynthGenConfig,
    generate_sample,
    synthesize_batch,
    FAULT_DEFS,
)
from data_dog.synth.synth_eval import build_samples, run_synth_eval, summarize_synth


def _cfg(fault, seed=0, joint="joint_motor.front_left_hip"):
    return SynthGenConfig(
        n_joints=2,
        joints=["joint_motor.front_left_hip", "joint_motor.front_right_hip"],
        n_seconds=120.0,
        dt=0.01,
        seed=seed,
        active_fault=None if fault == "normal" else fault,
        fault_component=joint if fault != "normal" else "",
    )


def test_normal_sample_is_benign():
    s = generate_sample(_cfg("normal", seed=1))
    t = s.telemetry("joint_motor.front_left_hip")
    # 正常：温度不越 75°C，电流不显著偏离参照，无丢包
    assert t["peak_temperature_c"] < 75.0
    assert t["packet_loss_rate"] == 0.0
    assert abs(t["metrics"]["current_a"] - t["reference_window_metrics"]["current_a"]) < 2.0
    # 正常样本无 ground_truth 故障
    assert s.ground_truth is None
    print("PASS test_normal_sample_is_benign")


def test_fault_samples_have_ground_truth_and_distinct_signatures():
    # 每种故障样本都应带 ground truth，且温度/电流/丢包信号可区分
    joints = ["joint_motor.front_left_hip", "joint_motor.front_right_hip"]
    for i, fault in enumerate(FAULT_DEFS):
        s = generate_sample(_cfg(fault, seed=100 + i))
        assert s.ground_truth is not None, f"{fault} 应有 ground truth"
        assert s.ground_truth["fault"] == fault
        assert s.ground_truth["component"] == joints[0]
        assert "window" in s.ground_truth  # 带"第几分钟"标签
    # 堵转/负载故障应显著抬温，丢包故障应有丢包率
    stall = generate_sample(_cfg("F1_stall", seed=200))
    loss = generate_sample(_cfg("F3_packet_loss", seed=201))
    assert stall.telemetry(joints[0])["peak_temperature_c"] > 90.0
    assert loss.telemetry(joints[0])["packet_loss_rate"] > 0.05
    print("PASS test_fault_samples_have_ground_truth_and_distinct_signatures")


def test_synth_eval_accuracy_against_ground_truth():
    """端到端：合成数据 → Agent 调查 → 对比 ground truth → 高准确率。"""
    results = run_synth_eval(n_joints=2, n_seconds=120.0, seed=0)
    s = summarize_synth(results)
    # 正常+5故障 = 6 条；预期准确率 >= 5/6
    assert s["n"] == 6, f"应生成 6 条样本（1正常+5故障），实际 {s['n']}"
    assert s["accuracy"] >= 5 / 6, f"准确率应 >= 5/6，实际 {s['accuracy']}"
    # 抽查：normal 应被 rejected，温度故障应被识别为需人工/确认
    by_fault = {r["fault"]: r for r in results}
    assert by_fault["normal"]["agent_disposition"] == "rejected"
    assert by_fault["F1_stall"]["agent_disposition"] in ("needs_human_review", "confirmed")
    print(f"PASS test_synth_eval_accuracy_against_ground_truth (accuracy={s['accuracy']})")


def test_build_samples_covers_all_faults():
    samples, labels = build_samples(2, 120.0, 0)
    faults = [l["fault"] for l in labels]
    assert faults == ["normal", "F1_stall", "F2_temp_rise",
                      "F3_packet_loss", "F4_current_bias", "F5_load_spike"]
    assert len(samples) == len(faults)
    print("PASS test_build_samples_covers_all_faults")
