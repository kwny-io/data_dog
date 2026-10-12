"""证据**不得泄露 ground truth / 原始掩码**：这是评测可信度的底线。

为什么要有它
-----------
`SynthSample.telemetry()` 的返回里一直带着 `fault_ground_truth`（含 `fault` / `severity` /
`desc`）与 `valid_mask`（12000 点逐点掩码）。此前没人发现，是因为**确定性 provider 不读
这两个字段**；但真 LLM 评测会把整包 JSON 喂给模型——那等于把答案连同 2 万 tokens 一起送进去。
本文件把边界钉住：**证据源返回给 Agent 的 payload 里不许有 GT，也不许有原始掩码**。

约定：每个 test 结尾 print("PASS <name>")。
"""
from __future__ import annotations

import json

from data_dog.synth import SynthGenConfig, synthesize_batch
from data_dog.synth.evidence_source import SyntheticEvidenceSource

JOINT = "joint_motor.front_left_hip"


def _sample(fault: str = "F2_temp_rise"):
    cfg = SynthGenConfig(n_joints=1, joints=[JOINT], n_seconds=120.0, dt=0.01,
                         seed=7, active_fault=None if fault == "normal" else fault,
                         fault_component="" if fault == "normal" else JOINT)
    return synthesize_batch([cfg])[0]


def test_evidence_has_no_ground_truth():
    s = _sample("F2_temp_rise")
    res = SyntheticEvidenceSource([s]).query_telemetry("d", JOINT, "09:55", "10:00")
    blob = json.dumps(res, ensure_ascii=False)
    assert "fault_ground_truth" not in res
    assert "ground_truth" not in blob
    assert "F2_temp_rise" not in blob, "标签值本身也不许出现在证据里"
    assert "热退化" not in blob, "标签描述也不许出现"
    print("PASS test_evidence_has_no_ground_truth")


def test_evidence_drops_raw_mask_but_keeps_useful_fields():
    s = _sample("F3_packet_loss")
    res = SyntheticEvidenceSource([s]).query_telemetry("d", JOINT, "09:55", "10:00")
    blob = json.dumps(res, ensure_ascii=False)
    assert "valid_mask" not in res, "逐点掩码不许进证据（2 万 tokens）"
    # 有用证据仍在：窗口指标 / 参照窗 / 峰值 / 丢包率
    for k in ("metrics", "reference_window_metrics", "peak_temperature_c", "packet_loss_rate"):
        assert k in res, k
    assert len(blob) < 4000, f"证据 payload 仍过大：{len(blob)} 字符"
    print("PASS test_evidence_drops_raw_mask_but_keeps_useful_fields")
