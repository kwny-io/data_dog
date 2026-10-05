"""真实传感/执行链噪声模型：真值 → 各噪声层 → 观测。

为什么需要
----------
裸 MuJoCo 输出是**理想化**的（无限精度、零延迟、无损总线）。真实机器人遥测里，同样的
物理量会被编码器量化、齿槽转矩、控制延迟、电池跌落、电流测量误差、热噪声、总线丢包等
污染。评估一个"故障调查 Agent"时，如果喂给它的数据比现场干净得多，测出的准确率就是
虚高的。所以本模块在真值上叠这些噪声，**让评估贴近现场**。

诚实边界（重要）
----------------
**下列参数绝大多数是"假设值"，不是实测标定值**（除编码器位数按 Go2 ~14bit 有效位给的
量级外）。它们标注为 `ASSUMPTION`，用于让数据"像真的"，**不构成对具体机型的测量结论**。
所有用到的参数都会写进 manifest，便于日后用真机数据回灌替换。

设计
----
`apply_noise(sim, cfg, rng) -> (SimResult, list[dict])`：不原地改输入，返回新 SimResult
与被激活噪声的记录（供 manifest 审计）。作用顺序刻意固定，保证可复现。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class NoiseConfig:
    """噪声总开关与参数。默认全关（`enable=False`）→ 与旧行为完全一致。"""
    enable: bool = False

    # 编码器量化：Δq = 2π / 2^bits  （ASSUMPTION: Go2 关节有效 ~14 bit）
    quantize_bits: int = 14

    # 齿槽转矩 cogging：τ += A·sin(N_slot·θ + φ)，每关节固定随机初相
    cog_amp: float = 0.5        # N·m （ASSUMPTION: ~BLDC 额定的 1-3%）
    cog_slots: int = 14         # 每转齿槽数 （ASSUMPTION）

    # 控制-采样延迟：命令延迟 d 步
    latency_steps: int = 1      # 1 步 = dt（ASSUMPTION: ~10ms 控制周期）

    # 采样抖动：以概率 jitter_p 把该行时间戳索引平移 ±1 步
    jitter_p: float = 0.0

    # 电池 sag：V_bus = V_oc - I_tot·R_int，I_tot 取 12 关节电流之和
    v_oc: float = 48.0          # 开路电压 V（48V 平台）
    r_int: float = 0.03         # 内阻 Ω （ASSUMPTION: 0.02-0.05）

    # 电流测量：I_obs = (1+g)·I_true + b + ε；g/b 每 trial 固定，ε 每步随机
    cur_gain_sigma: float = 0.02   # g ~ N(0, .)
    cur_bias_sigma: float = 0.10   # b ~ N(0, .) A
    cur_noise_a: float = 0.05      # ε ~ N(0, .) A

    # 热噪声：观测 T_obs = T + N(0, σ_T)；过程 dT += σ_p·√dt·N(0,1)
    temp_obs_sigma: float = 0.5    # °C （ASSUMPTION）
    temp_proc_sigma: float = 0.3   # °C/√s （ASSUMPTION）

    # 总线丢包：每步每关节有效概率 1-p
    loss_p: float = 0.0

    def active(self) -> list[str]:
        """返回实际开启的噪声名（enable=False 时为空）。"""
        if not self.enable:
            return []
        names = ["quantize", "latency"]
        if self.cog_amp > 0:
            names.append("cogging")
        if self.jitter_p > 0:
            names.append("jitter")
        if self.r_int > 0:
            names.append("battery_sag")
        if self.cur_noise_a > 0 or self.cur_bias_sigma > 0 or self.cur_gain_sigma > 0:
            names.append("current_meas")
        if self.temp_obs_sigma > 0 or self.temp_proc_sigma > 0:
            names.append("thermal")
        if self.loss_p > 0:
            names.append("packet_loss")
        return names


def _quantize(x: np.ndarray, step: float) -> np.ndarray:
    return np.round(x / step) * step


def apply_noise(sim, cfg: NoiseConfig, rng: np.random.Generator):
    """在 SimResult 真值上叠加噪声，返回 (新 SimResult, 激活噪声记录列表)。

    不修改入参；`sim` 需为 `simulate.SimResult`（鸭子类型：有 pos/vel/torque/
    current/temperature/base_pos/ts）。
    """
    from .simulate import SimResult  # 局部导入避免循环

    if not cfg.enable:
        return sim, []

    n, nj = sim.pos.shape
    rec: list[dict] = []

    # --- 顺序 1：量化（位置/速度）---
    dq = 2.0 * np.pi / (2 ** cfg.quantize_bits)
    pos = _quantize(sim.pos, dq)
    vel = _quantize(sim.vel, dq)  # 同量化步长近似（rad/s）
    rec.append({"noise": "quantize", "dq_rad": dq, "bits": cfg.quantize_bits})

    # --- 顺序 2：延迟（命令侧，近似为对真值数组做环形平移）---
    d = int(max(0, cfg.latency_steps))
    if d > 0 and n > d:
        pos = np.vstack([np.repeat(pos[:1], d, axis=0), pos[:-d]])
        vel = np.vstack([np.repeat(vel[:1], d, axis=0), vel[:-d]])
    rec.append({"noise": "latency", "steps": d, "dt": float(sim.ts[1] - sim.ts[0]) if n > 1 else 0.0})

    # --- 顺序 3：齿槽转矩（叠加到力矩，再据力矩反推电流）---
    torque = sim.torque.copy()
    if cfg.cog_amp > 0:
        phases = rng.uniform(0, 2 * np.pi, size=nj)
        cog = cfg.cog_amp * np.sin(cfg.cog_slots * sim.pos + phases[None, :])
        torque = torque + cog
        rec.append({"noise": "cogging", "amp_nm": cfg.cog_amp, "slots": cfg.cog_slots})

    # --- 顺序 4：抖动（时间戳索引平移）---
    if cfg.jitter_p > 0 and n > 2:
        shift = rng.random(n) < cfg.jitter_p
        jpos = pos.copy()
        jpos[1:-1][shift[1:-1]] = pos[:-2][shift[1:-1]]  # 用前一步值近似抖动
        pos = jpos
        rec.append({"noise": "jitter", "p": cfg.jitter_p})

    # --- 顺序 5：电池 sag（据总电流算母线电压，写回 SimResult.v_bus）---
    kt = 0.25  # 与 MOTOR_PARAMS 一致；由 |torque|/kt 得每关节电流
    current_true = np.abs(torque) / kt
    v_bus = np.full(n, cfg.v_oc)
    if cfg.r_int > 0:
        i_tot = current_true.sum(axis=1)
        # 现实下限：母线不会跌到 0（BMS 会限流/断电）。取 0.7·V_oc 作工程下限，
        # 避免出现对 48V 平台不物理的个位数电压。
        v_bus = np.maximum(cfg.v_oc - i_tot * cfg.r_int, 0.7 * cfg.v_oc)
        rec.append({"noise": "battery_sag", "v_oc": cfg.v_oc, "r_int": cfg.r_int,
                    "v_min": float(v_bus.min()),
                    "floor_v": 0.7 * cfg.v_oc})

    # --- 顺序 6：电流测量误差 ---
    current = current_true.copy()
    if cfg.cur_noise_a > 0 or cfg.cur_bias_sigma > 0 or cfg.cur_gain_sigma > 0:
        g = rng.normal(0, cfg.cur_gain_sigma)
        b = rng.normal(0, cfg.cur_bias_sigma)
        eps = rng.normal(0, cfg.cur_noise_a, size=current.shape)
        current = (1 + g) * current_true + b + eps
        rec.append({"noise": "current_meas", "gain": float(g), "bias_a": float(b),
                    "eps_sigma": cfg.cur_noise_a})

    # --- 顺序 7：热噪声（过程 + 观测）---
    temp = sim.temperature
    if cfg.temp_proc_sigma > 0 or cfg.temp_obs_sigma > 0:
        dt = float(sim.ts[1] - sim.ts[0]) if n > 1 else 0.01
        proc = rng.normal(0, cfg.temp_proc_sigma * np.sqrt(dt), size=temp.shape)
        temp = temp + np.cumsum(proc, axis=0)
        temp = temp + rng.normal(0, cfg.temp_obs_sigma, size=temp.shape)
        rec.append({"noise": "thermal", "obs_sigma": cfg.temp_obs_sigma,
                    "proc_sigma": cfg.temp_proc_sigma})

    # --- 顺序 8：总线丢包（NaN 标记无效步，由下游按 validmask 处理）---
    valid = np.ones((n, nj), dtype=bool)
    if cfg.loss_p > 0:
        valid = rng.random((n, nj)) >= cfg.loss_p
        rec.append({"noise": "packet_loss", "p": cfg.loss_p,
                    "loss_rate": float(1.0 - valid.mean())})

    out = SimResult(
        joints=sim.joints, ts=sim.ts.copy(),
        pos=pos, vel=vel, torque=torque, current=current, temperature=temp,
        base_pos=sim.base_pos.copy() if sim.base_pos.size else sim.base_pos,
        base_quat=getattr(sim, "base_quat", np.zeros((0, 4))),
        v_bus=v_bus, valid=valid,
    )
    return out, rec


__all__ = ["NoiseConfig", "apply_noise"]
