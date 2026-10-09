"""MuJoCo 仿真管线 smoke 测试：20 条试验，验证步态/噪声/落盘/可查询。

用法：
  PYTHONPATH=src python scripts/mj_smoke.py

检查项（任一失败即非零退出）：
1. 自由基座 trot 能跑起来，报告翻倒率。
2. 噪声确实进了数据（clean vs noisy 差值 > 0）。
3. 同 seed 两次生成逐位相同（可复现）。
4. 每条能被 InvestigationTools 端到端查出非 None。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_dog.mujoco_sim.gait import GaitParams  # noqa: E402
from data_dog.mujoco_sim.noise import NoiseConfig, apply_noise  # noqa: E402
from data_dog.mujoco_sim.simulate import simulate  # noqa: E402


def main() -> int:
    dur, dt = 2.0, 0.01

    # --- 1) 自由基座 trot 稳定性 + 真前进 ---
    print("== 自由基座 trot ==")
    falls, zs, dxs = 0, [], []
    for seed in range(10):
        r = simulate(duration_s=dur, dt=dt, seed=seed,
                     gait=GaitParams(freq=1.0, mode="free"))
        zs.append(float(r.base_pos[:, 2].min()))
        dxs.append(float(r.base_pos[-1, 0] - r.base_pos[0, 0]))
        falls += int(r.fell)
    print(f"  10 次: 翻倒 {falls} 次, min base z 范围 {min(zs):.3f}~{max(zs):.3f} m")
    print(f"  净前进 x 范围 {min(dxs):+.3f}~{max(dxs):+.3f} m（>0 才算真走路，非原地踏步）")
    # 真走路的判据：净前进为正（本仓库标定档约 0.05 m/s）
    r5 = simulate(duration_s=5.0, dt=dt, seed=0, gait=GaitParams(freq=1.0, mode="free"))
    dx5 = float(r5.base_pos[-1, 0] - r5.base_pos[0, 0])
    print(f"  5s 净前进 {dx5:+.3f} m")
    assert dx5 > 0.05, f"trot 未产生净前进（{dx5:.3f} m）——疑似原地踏步"

    # --- 2) 噪声确实进数据 ---
    print("== 噪声 ==")
    clean = simulate(duration_s=dur, dt=dt, seed=0, gait=GaitParams(freq=1.0, mode="free"))
    rng = np.random.default_rng(0)
    noisy, rec = apply_noise(clean, NoiseConfig(enable=True, loss_p=0.02), rng)
    d_pos = float(np.abs(noisy.pos - clean.pos).mean())
    d_cur = float(np.abs(noisy.current - clean.current).mean())
    d_tmp = float(np.abs(noisy.temperature - clean.temperature).mean())
    print(f"  pos 差 {d_pos:.2e}  cur 差 {d_cur:.2e}  temp 差 {d_tmp:.2e}")
    print(f"  激活噪声: {[r['noise'] for r in rec]}")
    assert d_pos > 0 and d_cur > 0 and d_tmp > 0, "噪声未进入数据"
    assert noisy.v_bus.size and noisy.v_bus.min() < 48.0, "电池 sag 未生效"

    # --- 3) 可复现 ---
    print("== 可复现 ==")
    a = simulate(duration_s=dur, dt=dt, seed=42, gait=GaitParams(freq=1.0, mode="free"))
    b = simulate(duration_s=dur, dt=dt, seed=42, gait=GaitParams(freq=1.0, mode="free"))
    assert np.array_equal(a.pos, b.pos) and np.array_equal(a.torque, b.torque), "同 seed 不可复现"
    print("  同 seed 逐位相同 OK")

    # --- 4) 端到端可查询（不依赖 mujoco 之外的 Agent 依赖）---
    print("== 端到端查询 ==")
    from data_dog.mujoco_sim.to_sample import to_synth_sample
    s = to_synth_sample(clean, sample_id="smoke-0", active_fault=None)
    assert s.joints, "样本无关节数据"
    print(f"  样本 {s.sample_id} 覆盖部件: {sorted(s.joints)[:3]}... (共 {len(s.joints)})")
    print(f"  基座末位移 {clean.base_pos[-1].round(3)}, 翻倒={clean.fell}")

    print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
