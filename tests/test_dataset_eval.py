"""数据集评估（`synth/dataset_eval.py`）的口径测试。

这份评估器产出 README 里的头条数字（10 万条忠实 MuJoCo 数据集、分层抽 n=2000、
accuracy 0.818 / Wilson 95% CI），但此前**没有任何测试覆盖**。本文件锁住它的
"观测口径"——分层抽样、Wilson 区间、命中统计与混淆矩阵——而不重跑 MuJoCo 仿真
（`replay` / `eval_one` 用替身注入），故毫秒级、全离线确定性。

为什么值得单独锁：口径错了会让分数**虚高或虚低**（历史事故：参照窗与故障窗重叠
稀释偏置；证据白名单漏部件导致"无证据"分支对故障样本免费命中）。数字可以变，
口径不能悄悄变。
"""
import json

import pytest

from data_dog.synth import dataset_eval as de


def _rec(trial_id: str, fault: str | None, severity: float,
         component: str = "joint_motor.front_left_knee", fell: bool = False) -> dict:
    """一条 manifest 记录（只含评估路径真正读到的字段）。"""
    return {
        "trial_id": trial_id, "seed": 1, "freq": 100, "duration_s": 2.0, "dt": 0.01,
        "payload_kg": 0.0, "slope_deg": 0.0, "t_amb": 25.0, "v_bus_oc": 28.8,
        "fault": fault, "severity": severity,
        "component": component if fault else "", "fell": fell,
        "gait_mode": "free",
    }


def test_severity_bucket_boundaries():
    # 正常（无故障）走 "none" 层；亚阈值单独成层（制造真实负例）；其余 "full"
    assert de._severity_bucket(0.0) == "none"
    assert de._severity_bucket(None) == "none"
    assert de._severity_bucket(0.3) == "sub"
    assert de._severity_bucket(0.599) == "sub"
    assert de._severity_bucket(0.6) == "full"
    assert de._severity_bucket(1.0) == "full"
    print("PASS test_severity_bucket_boundaries")


def test_stratified_sample_keeps_strata_and_is_deterministic():
    recs = (
        [_rec(f"n{i}", None, 0.0) for i in range(40)]
        + [_rec(f"f1_{i}", "F1_stall", 0.8) for i in range(10)]
        + [_rec(f"f4_{i}", "F4_current_bias", 0.3) for i in range(10)]
    )
    picked = de.stratified_sample(recs, n=20, seed=7)

    # 1) 抽样量不超过 n，且每条都来自输入（不造样本）
    assert len(picked) <= 20
    assert {r["trial_id"] for r in picked} <= {r["trial_id"] for r in recs}

    # 2) 分层保住：normal 层与两个故障层、以及 sub/full 两种严重度都被抽到，
    #    否则误报率/逐类召回无从谈起（这是分层抽样的存在理由）
    strata = {(r["fault"], de._severity_bucket(r["severity"])) for r in picked}
    assert (None, "none") in strata
    assert ("F1_stall", "full") in strata
    assert ("F4_current_bias", "sub") in strata

    # 3) 同种子可复现（评估数字要能被别人重跑出来）
    again = de.stratified_sample(recs, n=20, seed=7)
    assert [r["trial_id"] for r in again] == [r["trial_id"] for r in picked]
    # 换种子应换一批（不依赖偶然）
    other = de.stratified_sample(recs, n=20, seed=8)
    assert [r["trial_id"] for r in other] != [r["trial_id"] for r in picked]
    print("PASS test_stratified_sample_keeps_strata_and_is_deterministic")


def test_wilson_ci_known_values_and_bounds():
    # n=0 不出 NaN（未跑任何样本时 CI 为空区间，而非崩）
    assert de.wilson_ci(0, 0) == (0.0, 0.0)

    # 经典小样本值：0/10 与 10/10 的 Wilson 95% 区间上/下界互为镜像
    lo0, hi0 = de.wilson_ci(0, 10)
    lo1, hi1 = de.wilson_ci(10, 10)
    assert lo0 == 0.0 and abs(hi0 - 0.2775) < 1e-3
    assert hi1 == 1.0 and abs(lo1 - 0.7225) < 1e-3

    # 区间始终含点估计、落在 [0,1]、且命中数越多下界越高（单调）
    prev_lo = -1.0
    for hit in range(0, 21):
        lo, hi = de.wilson_ci(hit, 20)
        assert 0.0 <= lo <= hit / 20 <= hi <= 1.0
        assert lo >= prev_lo
        prev_lo = lo
    print("PASS test_wilson_ci_known_values_and_bounds")


def test_load_manifest_reads_all_shards_and_fails_closed(tmp_path):
    # 无 shard → 明确报错（而不是拿空集算出 accuracy 0 的假结论）
    with pytest.raises(FileNotFoundError):
        de.load_manifest(str(tmp_path))

    (tmp_path / "shard_00001.jsonl").write_text(
        json.dumps(_rec("b", "F1_stall", 0.8)) + "\n", encoding="utf-8")
    (tmp_path / "shard_00000.jsonl").write_text(
        json.dumps(_rec("a", None, 0.0)) + "\n"
        + json.dumps(_rec("c", "F5_load_spike", 0.9)) + "\n", encoding="utf-8")
    recs = de.load_manifest(str(tmp_path))
    # 按文件名排序读入（保序），空行被跳过
    assert [r["trial_id"] for r in recs] == ["a", "c", "b"]
    print("PASS test_load_manifest_reads_all_shards_and_fails_closed")


def test_run_dataset_eval_scoring_caliber(tmp_path, monkeypatch):
    """锁住命中统计口径：accuracy / 逐类 recall / normal 误报率 / 混淆矩阵 / 翻倒剔除。"""
    recs = [
        _rec("n0", None, 0.0), _rec("n1", None, 0.0),          # normal → 期望 rejected
        _rec("s0", "F1_stall", 0.8), _rec("s1", "F1_stall", 0.9),
        _rec("b0", "F4_current_bias", 0.3),
        _rec("fell0", "F1_stall", 0.8, fell=True),             # 翻倒 → 先剔除，不进分母
    ]
    (tmp_path / "shard_00000.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")

    # 替身：不跑仿真、不跑 Agent，只按 ground truth 决定处置，制造已知的混淆矩阵
    # （label id 由 _label 加 `mj_` 前缀）
    disposition = {"mj_n0": "rejected", "mj_n1": "confirmed",      # n1 = 误报
                   "mj_s0": "needs_human_review", "mj_s1": "needs_human_review",
                   "mj_b0": "rejected"}                            # b0 = 漏报
    monkeypatch.setattr(de, "replay", lambda rec: object())

    def fake_eval_one(sample, label, provider, cand=None):
        d = disposition[label["id"]]
        return {"fault": label["fault"], "ground_truth": label["expected"],
                "agent_disposition": d, "hit": d == label["expected"]}

    monkeypatch.setattr(de, "eval_one", fake_eval_one)

    out = de.run_dataset_eval(str(tmp_path), n=100, seed=7, provider=object())

    assert out["sampled"] == 6 and out["skipped_fell"] == 1   # 翻倒被剔除、仍如实上报
    assert out["n"] == 5                                      # 分母不含翻倒
    assert out["accuracy"] == 0.6                             # 3 命中 / 5
    assert out["by_fault"]["F1_stall"] == {"n": 2, "recall": 1.0}
    assert out["by_fault"]["F4_current_bias"] == {"n": 1, "recall": 0.0}
    assert out["by_fault"]["normal"] == {"n": 2, "fp_rate": 0.5}
    assert out["by_fault"]["F2_temp_rise"] == {"n": 0, "recall": 0.0}   # 空层不崩
    assert out["confusion"]["rejected"] == {"rejected": 1, "confirmed": 1}
    assert out["confusion"]["needs_human_review"] == {"needs_human_review": 2, "rejected": 1}
    assert out["gait_mode"] == "free"
    # Wilson 区间必须与实际 n 对齐（下界 ≤ 0.6 ≤ 上界）
    lo, hi = out["wilson_95ci"]
    assert lo <= out["accuracy"] <= hi
    print("PASS test_run_dataset_eval_scoring_caliber")


def test_workers_parallelize_only_the_judgement_never_the_replay(tmp_path, monkeypatch):
    """`--workers` 的两条硬约束（真 LLM 那栏靠它才跑得完，但不能拿正确性换速度）：

    1. **回放必须始终串行**：`mujoco.MjModel/MjData` 实例不是线程安全的，并发回放会
       **静默**产出错值——而错值会直接污染对外发布的准确率。这里用并发峰值断言钉住。
    2. **并行结果必须与串行逐条一致**：`ThreadPoolExecutor.map` 保序，聚合口径不受影响。
    """
    import threading
    import time as _time

    recs = [_rec(f"r{i}", "F1_stall" if i % 2 else None, 0.5) for i in range(8)]
    (tmp_path / "shard_00000.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")

    lock = threading.Lock()
    live = {"n": 0, "max": 0}

    def fake_replay(rec):
        with lock:
            live["n"] += 1
            live["max"] = max(live["max"], live["n"])
        _time.sleep(0.01)          # 拉长临界区，让并发真有机会撞上
        with lock:
            live["n"] -= 1
        return object()

    def fake_eval_one(sample, label, provider, cand=None):
        _time.sleep(0.01)          # 模拟"等 LLM"（这才是可以并行的那段）
        return {"fault": label["fault"], "ground_truth": label["expected"],
                "agent_disposition": label["expected"], "hit": True}

    monkeypatch.setattr(de, "replay", fake_replay)
    monkeypatch.setattr(de, "eval_one", fake_eval_one)

    serial = de.run_dataset_eval(str(tmp_path), n=100, seed=7,
                                 provider=object(), workers=1)
    assert live["max"] == 1, "串行档下回放竟然并发了"

    live["max"] = 0
    parallel = de.run_dataset_eval(str(tmp_path), n=100, seed=7,
                                   provider=object(), workers=4)

    assert live["max"] == 1, (
        f"回放出现并发（峰值 {live['max']}）——MuJoCo 非线程安全，会静默产出错值")
    assert parallel == serial, "并行结果与串行不一致（map 未保序，或聚合受顺序影响）"
    print("PASS test_workers_parallelize_only_the_judgement_never_the_replay")
