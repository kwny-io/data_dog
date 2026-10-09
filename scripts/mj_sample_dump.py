"""导出若干条含完整遥测的样例（正常 + 各故障），供人工目视判断"数据像不像真的"。

用法：
  PYTHONPATH=src python scripts/mj_sample_dump.py --dur 60

对每种故障各取一条，打印：
  - 关键工况（环温/负载/步频/severity/部件）
  - 温度、电流：正常窗 vs 故障窗 的均值/峰值
  - ASCII 迷你轨迹（故障部件 vs 一条正常部件对照）
纯文本，不依赖绘图库。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_dog.mujoco_sim.gait import GaitParams  # noqa: E402
from data_dog.mujoco_sim.noise import NoiseConfig, apply_noise  # noqa: E402
from data_dog.mujoco_sim.simulate import simulate  # noqa: E402
from data_dog.mujoco_sim.to_sample import to_synth_sample  # noqa: E402

_BLOCKS = "▁▂▃▄▅▆▇█"


def spark(vals: np.ndarray, width: int = 60) -> str:
    v = np.asarray(vals, dtype=float)
    v = v[~np.isnan(v)]
    if v.size == 0:
        return "(全 NaN)"
    if v.size > width:                      # 分箱取均值降采样
        k = v.size // width or 1
        v = np.array([np.nanmean(v[i:i + k]) for i in range(0, v.size, k)])[:width]
    lo, hi = np.nanmin(v), np.nanmax(v)
    if hi - lo < 1e-9:
        return _BLOCKS[0] * len(v)
    idx = np.clip(((v - lo) / (hi - lo) * (len(_BLOCKS) - 1)).round().astype(int),
                  0, len(_BLOCKS) - 1)
    return "".join(_BLOCKS[i] for i in idx)


def run_one(fault: str, dur: float, dt: float, seed: int, t_amb: float = 25.0):
    comp = "joint_motor.front_left_knee"
    g = GaitParams(freq=1.0, mode="free")
    sim = simulate(duration_s=dur, dt=dt, seed=seed, gait=g, t_amb=t_amb)
    rng = np.random.default_rng(seed ^ 0x5DEECE66D)
    sim, _ = apply_noise(sim, NoiseConfig(enable=True), rng)
    return to_synth_sample(sim, active_fault=None if fault == "normal" else fault,
                           fault_component=comp, severity=1.0, noise=False), comp


def cell_stats(sample, comp: str):
    j = sample.joints.index(comp)
    T = sample.temperature[:, j]
    cur = np.abs(sample.current[:, j])     # 电流（原名 I 触发 ruff E741 的歧义名）
    n = T.shape[0]
    a, b = int(n * 0.4), int(n * 0.9)      # 故障窗
    return {
        "T_norm": np.nanmean(T[:a]), "T_fault": np.nanmean(T[a:b]),
        "T_peak": np.nanmax(T), "I_norm": np.nanmean(cur[:a]),
        "I_fault": np.nanmean(cur[a:b]), "I_peak": np.nanmax(cur),
        "loss": 1.0 - np.nanmean(sample.valid_mask[:, j]),
        "T_series": T,
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--dur", type=float, default=60.0)
    ap.add_argument("--dt", type=float, default=0.01)
    args = ap.parse_args()

    print(f"===== MuJoCo 忠实数据样例（dur={args.dur}s, dt={args.dt}s, 自由基座 trot）=====\n")
    for i, fault in enumerate(["normal", "F1_stall", "F2_temp_rise",
                               "F3_packet_loss", "F4_current_bias", "F5_load_spike"]):
        s, comp = run_one(fault, args.dur, args.dt, seed=100 + i)
        st = cell_stats(s, comp)
        print(f"── {fault:14} (部件 {comp.split('.')[-1]}, seed={100+i}) ──")
        print(f"   温度: 正常窗 {st['T_norm']:5.1f}°C → 故障窗 {st['T_fault']:5.1f}°C"
              f"   峰值 {st['T_peak']:5.1f}°C")
        print(f"   电流: 正常窗 {st['I_norm']:5.1f}A  → 故障窗 {st['I_fault']:5.1f}A"
              f"   峰值 {st['I_peak']:5.1f}A")
        print(f"   丢包率: {st['loss']:.1%}")
        print(f"   温度轨迹: {spark(st['T_series'])}  (低→高，{len(_BLOCKS)} 级)")
        print()
    print("说明：故障窗 = 试验后段 [40%,90%]。温度轨迹的抬升/抬升后回落即物理热惯性。")
    print("诚实边界：仿真≠真实；热模型一阶；噪声多为假设值。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
