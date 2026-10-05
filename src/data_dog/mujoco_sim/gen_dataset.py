"""批量生成 MuJoCo 试验数据集：ProcessPool 分片 + 断点续跑 + JSONL manifest。

用法：
  python -m data_dog.mujoco_sim.gen_dataset --trials 10000 --workers 8 --out data/mj
  python -m data_dog.mujoco_sim.gen_dataset --trials 1000000 --workers 24 --out data/mj --resume

存储策略（见 plan）
------------------
- **manifest.jsonl**：每条 trial 只记标量汇总（配置 + 标签 + t_peak/i_mean/loss_rate/
  fell/保真度档）≈300B → 100 万条 ≈300MB。全时序不物化（1M×~100KB≈100GB 不可接受）。
- 评估子集**按 (config, seed) 重放**仿真得到，保真且省盘。
- 每个 shard 一个 `.jsonl` + `.done/{shard}.ok` 原子标记；`--resume` 跳过已完成分片。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import numpy as np

from .conditions import SamplingSpec, TrialConfig, config_from_dict, make_configs
from .gait import GaitParams
from .simulate import simulate
from .to_sample import to_synth_sample

# ---- worker：每进程复用已加载的 MjModel（simulate._MODEL_CACHE 是进程内的）----


def run_trial(cfg: TrialConfig) -> dict:
    """跑一条 trial，返回 manifest 记录（含标量汇总，不含全时序）。"""
    sim_raw = simulate(
        duration_s=cfg.duration_s, dt=cfg.dt, seed=cfg.seed,
        gait=cfg.gait(), payload_kg=cfg.payload_kg, slope_deg=cfg.slope_deg,
        t_amb=cfg.t_amb, v_bus_oc=cfg.v_bus_oc,
        wear_friction=cfg.wear_friction, wear_coulomb=cfg.wear_coulomb,
        wear_rth=cfg.wear_rth,
    )
    rng = np.random.default_rng(cfg.seed ^ 0x5DEECE66D)
    # 显式叠噪声，得到 noisy sim（含 v_bus / 丢包掩码），再在其上注入故障
    from .noise import NoiseConfig, apply_noise
    sim, _noise_rec = apply_noise(sim_raw, NoiseConfig(enable=True, v_oc=cfg.v_bus_oc), rng)
    sample = to_synth_sample(
        sim, sample_id=f"mj_{cfg.trial_id}",
        active_fault=cfg.fault, fault_component=cfg.component,
        severity=cfg.severity, noise=False,   # 已在上面加过噪
    )
    # 标量汇总（供 manifest；评估按需重放）
    temp = sample.temperature
    cur = sample.current
    valid = sample.valid_mask
    loss_rate = float(1.0 - np.nanmean(valid))
    rec = {
        "trial_id": cfg.trial_id,
        "seed": cfg.seed,
        "fault": cfg.fault,
        "severity": cfg.severity,
        "component": cfg.component,
        "gait_mode": cfg.gait_mode,
        "freq": cfg.freq,
        "payload_kg": cfg.payload_kg,
        "slope_deg": cfg.slope_deg,
        "t_amb": cfg.t_amb,
        "v_bus_oc": cfg.v_bus_oc,
        # 重放所需（评估时按 config 重跑仿真）
        "wear_friction": cfg.wear_friction,
        "wear_coulomb": cfg.wear_coulomb,
        "wear_rth": cfg.wear_rth,
        "duration_s": cfg.duration_s,
        "dt": cfg.dt,
        "fell": bool(sim.fell),
        "t_peak": float(np.nanmax(temp)),
        "t_rise": float(np.nanmax(temp) - cfg.t_amb),
        "i_mean": float(np.nanmean(np.abs(cur))),
        "i_peak": float(np.nanmax(np.abs(cur))),
        "loss_rate": loss_rate,
        "v_bus_min": float(sim.v_bus.min()) if sim.v_bus.size else cfg.v_bus_oc,
        "n_steps": int(sample.n_steps),
        "fidelity": "free_trot" if cfg.gait_mode == "free" else cfg.gait_mode,
    }
    return rec


def _run_shard(args: tuple) -> tuple[int, int]:
    """worker 入口：跑一个分片，写 shard jsonl + .done 标记。"""
    shard_id, cfgs_dicts, out_dir = args
    out = Path(out_dir)
    done = out / ".done"
    done.mkdir(parents=True, exist_ok=True)
    shard_path = out / f"shard_{shard_id:05d}.jsonl"
    if (done / f"{shard_id:05d}.ok").exists():
        return shard_id, -1  # 已存在，跳过
    tmp = shard_path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for d in cfgs_dicts:
            rec = run_trial(config_from_dict(d))
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, shard_path)                      # 原子落盘
    (done / f"{shard_id:05d}.ok").write_text("ok")
    return shard_id, len(cfgs_dicts)


def generate(trials: int, workers: int, out_dir: str, seed: int = 0,
             shard_size: int = 500, spec: SamplingSpec | None = None) -> str:
    spec = spec or SamplingSpec()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    from dataclasses import asdict
    cfgs = make_configs(trials, spec, seed=seed)
    shards = [cfgs[i:i + shard_size] for i in range(0, len(cfgs), shard_size)]
    jobs = [(sid, [asdict(c) for c in sh], str(out)) for sid, sh in enumerate(shards)]

    t0 = time.time()
    total = 0
    if workers <= 1:
        for j in jobs:
            sid, n = _run_shard(j)
            total += max(0, n)
            print(f"  shard {sid} done ({n})")
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_run_shard, j) for j in jobs]
            for k, fut in enumerate(as_completed(futs), 1):
                sid, n = fut.result()
                total += max(0, n)
                if k % 5 == 0 or k == len(futs):
                    el = time.time() - t0
                    print(f"  {k}/{len(futs)} shards, {total} trials, {el:.1f}s")

    dt = time.time() - t0
    print(f"完成：{trials} 条 / {len(shards)} 分片 / {workers} 进程 / {dt:.1f}s"
          f"（{trials/dt:.0f} trial/s）")
    print(f"manifest: {out}/shard_*.jsonl  （用 dataset_eval 读取）")
    return str(out)


def main():
    p = argparse.ArgumentParser(description="生成 MuJoCo 试验数据集")
    p.add_argument("--trials", type=int, default=10000)
    p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p.add_argument("--out", type=str, default="data/mj")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--shard-size", type=int, default=500)
    p.add_argument("--duration", type=float, default=2.0)
    p.add_argument("--gait-mode", type=str, default="free",
                   choices=["free", "suspended", "micro_swing"])
    args = p.parse_args()
    spec = SamplingSpec(duration_s=args.duration, gait_mode=args.gait_mode)
    generate(args.trials, args.workers, args.out, seed=args.seed,
             shard_size=args.shard_size, spec=spec)


if __name__ == "__main__":
    main()
