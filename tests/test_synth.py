"""Go2 合成遥测生成器 + ground truth 评估（含 synth_eval 纯逻辑）的测试。

验证：
- 合成样本物理自洽：正常样本温度/电流接近基线；故障样本有可区分的抬升。
- 5 类故障都被注入，且每份样本带 ground truth 标签（部件+窗口+故障类型）。
- 用合成数据跑 Agent 调查（确定性 provider），对比 ground truth，正常→rejected、
  故障→needs_human_review/confirmed，能得到高准确率。

为什么把 `synth_eval` 的用例也放这里
------------------------------------
`synth/synth_eval.py` 是"合成数据 → Agent 调查 → 对比 ground truth"的评估内循环，它的
纯逻辑（ground truth 期望表、确定性 provider 的阈值裁决、CLI 输出）与合成生成器同属一条
链路，放一起改口径时不容易漏。**不调用真实 LLM**：确定性 provider 全部离线可跑。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from data_dog.synth import (
    FAULT_DEFS,
    SynthGenConfig,
    generate_sample,
)
from data_dog.synth.synth_eval import (
    _ACCEPTABLE,
    _FAULT_EXPECTED,
    DeterministicSynthProvider,
    build_samples,
    eval_one,
    expected_vs_actual,
    main,
    run_synth_eval,
    summarize_synth,
)

ROOT = Path(__file__).resolve().parents[1]


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
    faults = [lb["fault"] for lb in labels]
    assert faults == ["normal", "F1_stall", "F2_temp_rise",
                      "F3_packet_loss", "F4_current_bias", "F5_load_spike"]
    assert len(samples) == len(faults)
    print("PASS test_build_samples_covers_all_faults")


# ---------------------------------------------------------------- synth_eval 纯逻辑

def test_eval_one_uses_requested_joint_and_labels():
    """`eval_one` 的候选部件取自样本关节；命中口径与 ground truth 一致（离线确定性）。"""
    samples, labels = build_samples(2, 5.0, 0)
    normal = [i for i, lb in enumerate(labels) if lb["fault"] == "normal"][0]
    res = eval_one(samples[normal], labels[normal], None, None)
    assert res["sample"] == labels[normal]["id"]
    assert res["component"] == samples[normal].joints[0], "默认用 0 号关节做候选"
    assert res["ground_truth"] == "rejected"
    assert res["agent_disposition"] == "rejected" and res["hit"] is True
    assert 0.0 <= res["confidence"] <= 1.0
    assert len(res["summary"]) <= 90, "摘要被截断到 90 字符（报告排版约束）"
    print("PASS test_eval_one_uses_requested_joint_and_labels")


def test_deterministic_provider_find_telemetry_edges():
    """`_find_telemetry` 从后往前找工具结果：坏 JSON / 缺字段 / 空内容都要跳过而非崩。"""
    prov = DeterministicSynthProvider()
    assert prov._find_telemetry([SimpleNamespace(role="user", content="hi")]) is None
    assert prov._find_telemetry([SimpleNamespace(role="tool", content="{not json")]) is None
    good = {"ok": True, "metrics": {"temperature_c": 1.0}}
    found = prov._find_telemetry([
        SimpleNamespace(role="tool", content=json.dumps({"ok": True})),
        SimpleNamespace(role="tool", content=""),
        SimpleNamespace(role="tool", content=json.dumps(good)),
        SimpleNamespace(role="tool", content="{broken"),
    ])
    assert found == good, "从最近一条往前找，跳过不合法/不完整的工具结果"
    print("PASS test_deterministic_provider_find_telemetry_edges")


def test_deterministic_provider_decide_thresholds():
    """确定性 provider 的物理阈值口径：无遥测→需复核；临界高温→confirmed；正常→rejected。"""
    prov = DeterministicSynthProvider()

    def tel(peak, cur=6.0, ref_t=None, ref_cur=None, loss=0.0):
        ref = {"temperature_c": peak if ref_t is None else ref_t,
               "current_a": cur if ref_cur is None else ref_cur}
        return {"ok": True, "metrics": {"temperature_c": peak, "current_a": cur},
                "peak_temperature_c": peak, "reference_window_metrics": ref,
                "packet_loss_rate": loss}

    assert prov._decide(None)["disposition"] == "needs_human_review", "无证据不得判正常"
    assert prov._decide(tel(95.0))["disposition"] == "confirmed"
    assert prov._decide(tel(80.0))["disposition"] == "needs_human_review"
    # 丢包阈值：docstring 写的是 >=0.1，代码实际是 >=0.05（记录差异，属注释滞后）
    assert prov._decide(tel(30.0, loss=0.06))["disposition"] == "needs_human_review"
    assert prov._decide(tel(30.0, loss=0.04))["disposition"] == "rejected"
    assert prov._decide(tel(30.0, cur=12.0, ref_cur=6.0))["disposition"] == "needs_human_review"
    assert prov._decide(tel(30.0, cur=7.0, ref_cur=6.0))["disposition"] == "rejected"

    # 环温口径：同一份 70°C 数据，环温 40°C 时是"显著抬升+升温趋势"，环温 45°C 时不是
    prov.ambient = 40.0
    assert prov._decide(tel(70.0, ref_t=60.0))["disposition"] == "needs_human_review"
    hot_env = DeterministicSynthProvider()
    hot_env.ambient = 45.0
    assert hot_env._decide(tel(70.0))["disposition"] == "rejected", (
        "硬编码 25°C 会把高环温误判成热故障；ambient 口径修正了这一点")
    print("PASS test_deterministic_provider_decide_thresholds")


def test_expected_vs_actual_and_summarize():
    """评估口径：正常→rejected；故障→needs_human_review/confirmed；空结果准确率为 0。"""
    assert expected_vs_actual("rejected", {"fault": "normal"})
    assert not expected_vs_actual("confirmed", {"fault": "normal"})
    assert not expected_vs_actual("needs_human_review", {"fault": "normal"})
    for f in FAULT_DEFS:
        assert expected_vs_actual("needs_human_review", {"fault": f})
        assert expected_vs_actual("confirmed", {"fault": f})
        assert not expected_vs_actual("rejected", {"fault": f})
    assert set(_ACCEPTABLE) == {"normal", *FAULT_DEFS}
    assert set(_FAULT_EXPECTED) == {"normal", *FAULT_DEFS}

    assert summarize_synth([]) == {"n": 0, "hit": 0, "accuracy": 0.0}
    assert summarize_synth([{"hit": True}, {"hit": False}]) == {"n": 2, "hit": 1,
                                                               "accuracy": 0.5}
    print("PASS test_expected_vs_actual_and_summarize")


def test_synth_eval_main_json_and_text(monkeypatch, capsys):
    """CLI 两条输出路径：`--json` 只吐汇总 JSON；默认打印逐条明细 + 汇总。"""
    monkeypatch.setattr(sys, "argv", ["synth_eval", "--json", "--n_seconds", "2"])
    assert main() == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["summary"]["n"] == 6
    assert len(payload["details"]) == 6

    monkeypatch.setattr(sys, "argv", ["synth_eval", "--n_seconds", "2"])
    assert main() == 0
    text = capsys.readouterr().out
    assert "合成数据 ground truth 评估" in text
    assert "汇总" in text and "准确率" in text
    print("PASS test_synth_eval_main_json_and_text")


def test_synth_eval_main_real_mode_offline(monkeypatch, capsys):
    """`--mode real`：无 key 时退出码 1；有 key 时用桩 Provider 跑通（全程不联网）。"""
    from data_dog.providers import client as client_mod
    from data_dog.providers import config as config_mod
    monkeypatch.setattr(config_mod, "load_config",
                        lambda: {"api_key": "", "vendor_id": "offline"})
    monkeypatch.setattr(sys, "argv", ["synth_eval", "--mode", "real", "--n_seconds", "2"])
    with pytest.raises(SystemExit) as ei:
        main()
    assert ei.value.code == 1
    assert "需要 data-dog init" in capsys.readouterr().out

    monkeypatch.setattr(config_mod, "load_config",
                        lambda: {"api_key": "fake-key", "vendor_id": "offline"})
    monkeypatch.setattr(client_mod.Provider, "from_config",
                        staticmethod(lambda vendor_id, api_key, **kw: DeterministicSynthProvider()))
    monkeypatch.setattr(sys, "argv", ["synth_eval", "--mode", "real", "--json",
                                      "--n_seconds", "2"])
    assert main() == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["summary"]["n"] == 6
    print("PASS test_synth_eval_main_real_mode_offline")


def test_synth_eval_main_tolerates_odd_stdout(monkeypatch):
    """stdout 替身没有 `reconfigure`、或 `reconfigure` 抛错，main() 都不得崩。

    这段 try/except 是为 Windows 控制台准备的（默认 cp936 装不下 ✓/⚠）。被重定向的
    stdout 替身（CI、日志收集器）恰好是它要兼容的场景，所以要两种都覆盖。
    """
    class _Stub:
        def __init__(self):
            self.text: list[str] = []

        def write(self, s):
            self.text.append(s)

        def flush(self):
            pass

    class _BrokenReconfigure(_Stub):
        def reconfigure(self, **kw):
            raise RuntimeError("不支持重配置")

    for stub in (_Stub(), _BrokenReconfigure()):
        monkeypatch.setattr(sys, "stdout", stub)
        monkeypatch.setattr(sys, "argv", ["synth_eval", "--json", "--n_seconds", "2"])
        assert main() == 0
        assert "summary" in "".join(stub.text)
    print("PASS test_synth_eval_main_tolerates_odd_stdout")


def test_synth_eval_main_guard_runs_as_main(monkeypatch, capsys):
    """`if __name__ == "__main__": sys.exit(main())` 守卫：以 `__main__` 身份跑一次。

    用 `runpy.run_path` 在**当前进程**里以 `__main__` 执行该文件：守卫行真的被执行（覆盖率
    也看得见），同时不必起子进程、也不会引发 runpy 的"模块已在 sys.modules"告警。下面另有
    一条真子进程用例负责"真实管道/UTF-8"那一面。
    """
    import runpy
    monkeypatch.setattr(sys, "argv",
                        ["data_dog.synth.synth_eval", "--json", "--n_seconds", "2"])
    with pytest.raises(SystemExit) as ei:
        runpy.run_path(str(ROOT / "src" / "data_dog" / "synth" / "synth_eval.py"),
                       run_name="__main__")
    assert ei.value.code == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["summary"]["n"] == 6
    print("PASS test_synth_eval_main_guard_runs_as_main")


def test_synth_eval_runs_as_module_cli():
    """以 `python -m` 跑真 CLI：真实管道上的 UTF-8 输出（✓/✗ 与中文）。

    本文件唯一启动子进程的用例（约 1 秒）。理由：`main()` 的 stdout 重配置只有在"真的被当
    脚本运行"时才会走真实管道（Windows 默认 cp936 装不下 ✓/✗ 与中文）。注意：子进程的执行
    **不计入**父进程的行覆盖率，所以守卫本身由上面那条 runpy 用例覆盖，这里专注"可当 CLI
    用 + 真管道不炸"这一对外承诺。
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    json_run = subprocess.run(
        [sys.executable, "-m", "data_dog.synth.synth_eval", "--json", "--n_seconds", "2"],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(ROOT),
        timeout=300, check=False)
    assert json_run.returncode == 0, json_run.stderr
    payload = json.loads(json_run.stdout.strip().splitlines()[-1])
    assert payload["summary"]["n"] == 6

    text_run = subprocess.run(
        [sys.executable, "-m", "data_dog.synth.synth_eval", "--n_seconds", "2"],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(ROOT),
        timeout=300, check=False)
    assert text_run.returncode == 0, text_run.stderr
    assert "汇总" in text_run.stdout and "✓" in text_run.stdout, "控制台编码坑：应为 UTF-8"
    print("PASS test_synth_eval_runs_as_module_cli")
