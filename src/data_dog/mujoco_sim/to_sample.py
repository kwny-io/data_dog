"""把 MuJoCo 仿真结果转成 synth 接口的 SynthSample，供现有 Agent 评估消费。

对接原理：
- `simulate.simulate()` 导出 12 关节的 pos/vel/torque/current/temperature。
- 现有 Agent 的 EvidenceStore 认识一组受限部件名（ALLOWED_COMPONENTS）：
     joint_motor.front_left_hip / front_right_hip / front_left_knee / front_right_knee
- 这里把 mujoco 的 12 关节映射到 Agent 认识的 4 个部件名（hip×2、knee×2，
  mujoco 里 calf 即 knee），转成与 `synth.SynthSample` 完全同构的对象，
  因此 `SyntheticEvidenceSource` / `AgentLoop` / `synth_eval` 无需改动即可消费。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import numpy as np

from .simulate import SimResult

if TYPE_CHECKING:                          # 仅供类型检查；运行期在函数内惰性导入
    from data_dog.synth.generator import SynthSample

# mujoco 执行器名 → Agent 认识的部件名。
# 选择依据（物理）：站立四足的 calf(≈knee) 因承重电流饱和(~90A)，无法体现"正常 vs 故障"的
# 差异；hip 偏摆几乎不发热。front thigh(大腿/屈髋) 有真实负载但不过饱和（基线 ~6A、有裕量），
# 是让"健康=正常电流、故障=异常"能被 Agent 判出的诚实关节。这里把 front thigh 映射到
# Agent 认识的 joint_motor.front_left_knee / front_right_knee。
_MUJOCO_TO_COMPONENT: dict[str, str] = {
    "FL_thigh": "joint_motor.front_left_knee",
    "FR_thigh": "joint_motor.front_right_knee",
}


def to_synth_sample(
    sim: SimResult,
    sample_id: str = "mj_go2",
    active_fault: str | None = None,
    fault_component: str = "joint_motor.front_left_hip",
) -> SynthSample:
    """从 SimResult 抽取出 Agent 认识的 4 个部件，构造一个 SynthSample。

    可选注入故障（配合 fault_component 制造异常遥测）：
      - F1_stall / F5_load_spike / F4_current_bias / F2_temp_rise / F3_packet_loss
    与 synth.generator 的故障语义保持一致，便于复用现有的 ground truth 期望表。
    """
    from data_dog.synth.generator import FAULT_DEFS, SynthSample

    components = list(_MUJOCO_TO_COMPONENT.values())  # 顺序固定
    # 找出这些部件在 sim.joints 里的列下标
    col = [sim.joints.index(a) for a, _ in _MUJOCO_TO_COMPONENT.items()]

    torque = sim.torque[:, col].copy()
    current = sim.current[:, col].copy()
    temperature = sim.temperature[:, col].copy()
    valid = np.ones_like(temperature, dtype=float)

    n_steps = torque.shape[0]
    dt = sim.ts[1] - sim.ts[0] if sim.ts.shape[0] > 1 else 0.01

    # ---- 可选故障注入（作用于 fault_component 对应列）----
    if active_fault in (None, "", "normal"):
        active_fault = None
    if active_fault and fault_component in components:
        j = components.index(fault_component)
    elif active_fault and components:
        # 回退到第一个阿部件
        j = 0
        fault_component = components[0]
    else:
        j = None

    if active_fault and j is not None:
        start_t = int(n_steps * 0.4)
        end_t = int(n_steps * 0.9)
        sl = slice(start_t, end_t)
        from data_dog.synth.generator import DEFAULT_PARAMS, Go2MotorModel
        model = Go2MotorModel(DEFAULT_PARAMS)

        if active_fault == "F1_stall":
            torque[sl, j] = DEFAULT_PARAMS["stall_torque"]
            current[sl, j] = min(DEFAULT_PARAMS["stall_torque"] / DEFAULT_PARAMS["kt"] + 2.0,
                                 torque[sl, j].max())
            for t in range(start_t, end_t):
                temperature[t, j] = model.step_temp(temperature[t - 1, j], current[t, j], dt)
        elif active_fault == "F5_load_spike":
            torque[sl, j] += DEFAULT_PARAMS["stall_torque"] * 0.6
            current[sl, j] = np.abs(torque[sl, j]) / DEFAULT_PARAMS["kt"] + 0.3
            for t in range(start_t, end_t):
                temperature[t, j] = model.step_temp(temperature[t - 1, j], current[t, j], dt)
        elif active_fault == "F4_current_bias":
            # 电流传感器零漂：故障窗内电流相对参照明显抬升（其余时段保持基线，
            # 让"参照=前半段"不被污染，Agent 能看出"窗口内电流抬升"）
            current[sl, j] += 5.0
            for t in range(start_t, end_t):
                temperature[t, j] = model.step_temp(temperature[t - 1, j], current[t, j], dt)
        elif active_fault == "F2_temp_rise":
            # 热退化/散热失效 + 持续大负载：故障窗内抬升电流并加大产热
            torque[sl, j] += DEFAULT_PARAMS["stall_torque"] * 0.25
            for t in range(start_t, end_t):
                c = np.abs(torque[t, j]) / DEFAULT_PARAMS["kt"] + 0.3
                current[t, j] = c
                # 散热恶化（Rth 变大）+ 大电流产热 → 温度在一个较长窗口里显著爬升
                temperature[t, j] = temperature[t - 1, j] + \
                    (c * c * DEFAULT_PARAMS["R"] - (temperature[t - 1, j] - DEFAULT_PARAMS["T_amb"])
                     / (DEFAULT_PARAMS["Rth"] * 1.6)) * dt / DEFAULT_PARAMS["Cth"]
        elif active_fault == "F3_packet_loss":
            rng = np.random.default_rng(0)
            drop: np.ndarray = np.asarray(rng.random(n_steps) < 0.15)
            drop[:start_t] = False
            drop[end_t:] = False
            valid[drop, j] = 0.0
            current[drop, j] = np.nan
            temperature[drop, j] = np.nan
            torque[drop, j] = np.nan

    # ---- ground truth（与 synth 一致）----
    gt = None
    if active_fault and fault_component:
        s_sec = int(n_steps * dt * 0.4)
        e_sec = int(n_steps * dt * 0.9)
        m1, s1 = divmod(s_sec, 60)
        m2, s2 = divmod(e_sec, 60)
        gt = {
            "sample_id": active_fault,
            "component": fault_component,
            "fault": active_fault,
            "severity": FAULT_DEFS[active_fault]["severity"],
            "window": f"{m1:02d}:{s1:02d}~{m2:02d}:{s2:02d}",
            "desc": FAULT_DEFS[active_fault]["desc"],
        }

    return SynthSample(
        sample_id=sample_id,
        joints=components,
        dt=dt,
        torque=torque,
        current=current,
        temperature=temperature,
        valid_mask=valid,
        ground_truth=gt,
    )


def make_mujoco_batch(
    faults: Sequence[str | None] | None = None,
    fault_component: str = "joint_motor.front_left_hip",
    duration_s: float = 2.0,
    dt: float = 0.01,
    seed: int = 0,
) -> list[SynthSample]:
    """跑多段 mujoco 仿真，产出 normal + 各故障 的样本批，供评估直接用。"""
    from .simulate import simulate

    if faults is None:
        faults = [None, "F1_stall", "F2_temp_rise", "F3_packet_loss",
                  "F4_current_bias", "F5_load_spike"]

    sim = simulate(duration_s=duration_s, dt=dt, seed=seed)
    samples: list[SynthSample] = []
    for i, f in enumerate(faults):
        s = to_synth_sample(
            sim, sample_id=f"mj_{f or 'normal'}_{i}",
            active_fault=f, fault_component=fault_component,
        )
        samples.append(s)
    return samples
