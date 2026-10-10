"""数据集评估：从百万级 MuJoCo manifest 里分层抽样，重放仿真，用现有 Agent 调查。

用法：
  python -m data_dog.synth.dataset_eval --manifest data/mj --n 2000 --seed 7 --json
  python -m data_dog.synth.dataset_eval --manifest data/mj --n 2000 --mode real  # 真 LLM

复用了 synth_eval 的评估口径（eval_one / expected_vs_actual），**不改判定语义**。
输出：总体 accuracy + 每故障 recall + normal 层误报率(FP) + 混淆矩阵 + Wilson 95% CI。

分层抽样（stratified）：按 (fault, severity_bucket) 分层，**强制包含 normal 层**，
否则无法评估误报率——这是这份数据存在的核心价值之一。

诚实边界：仿真数据 ≠ 真实数据；确定性 provider 的阈值口径来自合成/摆动档，
对 free_trot 档的适用性由评估结果本身暴露（低 accuracy 就是真实结论，不掩饰）。
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from data_dog.core.console import ensure_utf8_console  # noqa: E402
from data_dog.mujoco_sim.conditions import TrialConfig  # noqa: E402
from data_dog.mujoco_sim.noise import NoiseConfig, apply_noise  # noqa: E402
from data_dog.mujoco_sim.simulate import simulate  # noqa: E402
from data_dog.mujoco_sim.to_sample import to_synth_sample  # noqa: E402
from data_dog.schemas.events import CandidateEvent  # noqa: E402
from data_dog.synth.synth_eval import (  # noqa: E402
    DeterministicSynthProvider,
    eval_one,
)

FAULTS = ["F1_stall", "F2_temp_rise", "F3_packet_loss", "F4_current_bias", "F5_load_spike"]


def load_manifest(manifest_dir: str) -> list[dict]:
    """读入所有 shard_*.jsonl（保序：按文件名排序）。"""
    files = sorted(glob.glob(os.path.join(manifest_dir, "shard_*.jsonl")))
    if not files:
        raise FileNotFoundError(f"{manifest_dir} 下没有 shard_*.jsonl")
    recs: list[dict] = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    recs.append(json.loads(line))
    return recs


def _severity_bucket(s: float) -> str:
    if not s or s <= 0:
        return "none"      # 正常（无故障）层
    if s < 0.6:
        return "sub"       # 亚阈值（很可能检测不到 → 制造真实负例）
    return "full"


def stratified_sample(recs: list[dict], n: int, seed: int) -> list[dict]:
    """按 (fault, severity_bucket) 分层抽样，尽量保持各层比例。

    抽样在**翻倒剔除之前**做，便于统计 fell 比例；调用方随后再剔除 fell。
    """
    rng = np.random.default_rng(seed)
    strata: dict[tuple, list[dict]] = defaultdict(list)
    for r in recs:
        strata[(r["fault"], _severity_bucket(r["severity"]))].append(r)
    total = len(recs)
    picked: list[dict] = []
    for group in strata.values():
        k = min(max(1, round(n * len(group) / total)), len(group))
        idx = rng.choice(len(group), size=k, replace=False)
        picked.extend(group[i] for i in idx)
    rng.shuffle(picked)
    return picked[:n]


def replay(rec: dict) -> object:
    """按 manifest 记录重放仿真 → SynthSample（保真，且不依赖已物化的全时序）。"""
    cfg = TrialConfig(
        trial_id=rec["trial_id"], seed=rec["seed"], freq=rec["freq"],
        payload_kg=rec["payload_kg"], slope_deg=rec["slope_deg"], t_amb=rec["t_amb"],
        v_bus_oc=rec["v_bus_oc"], wear_friction=rec.get("wear_friction", 1.0),
        wear_coulomb=rec.get("wear_coulomb", 1.0), wear_rth=rec.get("wear_rth", 1.0),
        fault=rec["fault"], severity=rec["severity"], component=rec["component"],
        gait_mode=rec.get("gait_mode", "free"), duration_s=rec.get("duration_s", 2.0),
        dt=rec.get("dt", 0.01),
    )
    sim_raw = simulate(duration_s=cfg.duration_s, dt=cfg.dt, seed=cfg.seed,
                       gait=cfg.gait(), payload_kg=cfg.payload_kg, slope_deg=cfg.slope_deg,
                       t_amb=cfg.t_amb, v_bus_oc=cfg.v_bus_oc,
                       wear_friction=cfg.wear_friction, wear_coulomb=cfg.wear_coulomb,
                       wear_rth=cfg.wear_rth)
    rng = np.random.default_rng(cfg.seed ^ 0x5DEECE66D)
    sim, _ = apply_noise(sim_raw, NoiseConfig(enable=True, v_oc=cfg.v_bus_oc), rng)
    return to_synth_sample(sim, sample_id=f"mj_{cfg.trial_id}", active_fault=cfg.fault,
                           fault_component=cfg.component, severity=cfg.severity, noise=False)


class DatasetDeterministicProvider(DeterministicSynthProvider):
    """确定性 provider，但**第一步查候选事件自己的部件**（不硬编码 knee）。

    修掉现有 `MujocoDeterministicProvider` 硬编码 `front_left_knee` 的隐患：
    数据集里故障可落在 4 个不同部件上，provider 必须按候选部件查。
    """

    def __init__(self, component: str, t_amb: float = 25.0):
        super().__init__()
        self._component = component
        self.ambient = t_amb        # 供 _decide 算真实温升（不硬编码 25）

    def chat(self, messages, tools=None, model=None, temperature=0.2, max_tokens=2048):
        self.n += 1
        if self.n == 1:
            return {
                "kind": "tool_calls",
                "tool_calls": [{
                    "id": "s1", "name": "query_candidate_evidence",
                    "arguments": json.dumps({
                        "device_sn": "device-synth", "component": self._component,
                        "window_start": "09:55", "window_end": "10:00",
                    }),
                    "content": "",
                }],
                "__raw_tool_calls": [],
            }
        tel = self._find_telemetry(messages)
        return {"kind": "text", "content": json.dumps(self._decide(tel), ensure_ascii=False)}


def _make_candidate(rec: dict) -> CandidateEvent:
    return CandidateEvent(
        candidate_id=f"cand_{rec['trial_id']}",
        device_sn="device-synth",
        component_ref=rec["component"],
        signal="mj_dataset_anomaly",
        severity_hint="warning",
        window_start="09:55", window_end="10:00",
        sample_count=300, rule_matches=["MJ"],
        anomaly_score=0.5, detector_provenance="mujoco-dataset",
    )


def _label(rec: dict) -> dict:
    """ground truth 期望：正常→rejected；任何故障→needs_human_review。"""
    f = rec["fault"] or "normal"
    return {
        "id": f"mj_{rec['trial_id']}",
        "fault": f,
        "component": rec["component"] if f != "normal" else "",
        "expected": "rejected" if f == "normal" else "needs_human_review",
    }


def wilson_ci(hit: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% 置信区间（小样本/极端比例下比正态近似更稳）。

    极端比例（0 命中 / 全命中）直接返回精确端点：数学上命中 0 次时下界恰为 0、
    全命中时上界恰为 1，但 `max(0.0, center - half)` 会留下 ~1e-17 的浮点残差
    （实测 `wilson_ci(0, 11)` 的下界是 2.8e-17），破坏"下界 ≤ 点估计"这条不变量
    ——属性测试抓到的就是它。这里按定义取精确端点，而不是靠容差掩盖。
    """
    if n == 0:
        return (0.0, 0.0)
    p = hit / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    lo = 0.0 if hit == 0 else max(0.0, center - half)
    hi = 1.0 if hit == n else min(1.0, center + half)
    return (lo, hi)


def run_dataset_eval(manifest_dir: str, n: int = 2000, seed: int = 7,
                     mode: str = "offshelf", provider=None) -> dict:
    recs = load_manifest(manifest_dir)
    picked = stratified_sample(recs, n, seed)
    n_fell = sum(1 for r in picked if r.get("fell", False))
    sample = [r for r in picked if not r.get("fell", False)]

    results = []
    for rec in sample:
        s = replay(rec)
        cand = _make_candidate(rec)
        # offshelf：每条按自己的部件装配确定性 provider（按部件查询，不硬编码）
        prov = provider or DatasetDeterministicProvider(rec["component"], rec["t_amb"])
        r = eval_one(s, _label(rec), prov, cand=cand)
        results.append({**r, "severity": rec["severity"]})

    total = len(results)
    hit = sum(1 for r in results if r["hit"])
    acc = hit / total if total else 0.0
    ci = wilson_ci(hit, total)

    by_fault: dict[str, dict] = {}
    for f in FAULTS + ["normal"]:
        g = [r for r in results if r["fault"] == f]
        if f == "normal":
            fp = sum(1 for r in g if not r["hit"]) / len(g) if g else 0.0
            by_fault[f] = {"n": len(g), "fp_rate": round(fp, 3)}
        else:
            rec_ = sum(1 for r in g if r["hit"]) / len(g) if g else 0.0
            by_fault[f] = {"n": len(g), "recall": round(rec_, 3)}

    cm: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in results:
        cm[r["ground_truth"]][r["agent_disposition"]] += 1

    return {
        "n": total,
        "accuracy": round(acc, 4),
        "wilson_95ci": [round(ci[0], 4), round(ci[1], 4)],
        "by_fault": by_fault,
        "confusion": {k: dict(v) for k, v in cm.items()},
        "gait_mode": recs[0].get("gait_mode", "?") if recs else "?",
        "sampled": len(picked),
        "skipped_fell": n_fell,
    }


def _real_provider():
    from data_dog.providers.client import Provider
    from data_dog.providers.config import load_config
    cfg = load_config()
    if not cfg["api_key"]:
        print("real 模式需要先 `data-dog init` 配 key")
        sys.exit(1)
    return Provider.from_config(cfg["vendor_id"], cfg["api_key"])


def main():
    ensure_utf8_console()      # Windows 控制台默认 GBK，装不下 ⚠ 等字符
    p = argparse.ArgumentParser(description="MuJoCo 数据集 Agent 评估")
    p.add_argument("--manifest", type=str, required=True, help="含 shard_*.jsonl 的目录")
    p.add_argument("--n", type=int, default=2000)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--mode", choices=["offshelf", "real"], default="offshelf")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    prov = _real_provider() if args.mode == "real" else None
    out = run_dataset_eval(args.manifest, args.n, args.seed, args.mode, provider=prov)
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return
    print(f"数据集评估 —— gait={out['gait_mode']} n={out['n']} "
          f"accuracy={out['accuracy']}  Wilson95%CI={out['wilson_95ci']}")
    for f, m in out["by_fault"].items():
        print(f"  {f:<16} {m}")
    print("  混淆矩阵:", out["confusion"])
    print(f"  （抽样 {out['sampled']}，剔除翻倒 {out['skipped_fell']}）")
    print("\n⚠ 诚实边界：仿真数据≠真实数据；确定性 provider 阈值口径源自合成/摆动档，"
          "对 free_trot 档的适用性由本结果本身暴露（低分是真实结论，不掩饰）。")


if __name__ == "__main__":
    main()
