# -*- coding: utf-8 -*-
"""MuJoCo 运行 Go2 模型，导出物理自洽的关节遥测 + 电机温度。

目的：把 Go2 放进 mujoco（third_party/mujoco_menagerie/unitree_go2/scene.xml）跑一段
受控运动，记录每个关节的位置/速度/力矩，再用一阶热模型估算 12 个电机的温度。
输出的数据结构与 `synth/generator.SynthSample` 对齐——**MuJoCo 是合成数据源的上位替代，
Agent / 工具逻辑完全不用改**（见 README「数据从哪来」）。

命名约定与 go2.xml 一致：4 条腿 × hip/thigh/calf，执行器/关节顺序固定为
FL, FR, RL, RR。热模型参数复用 synth 里的 Go2 公开量级（kt / R / Cth / Rth / T_amb）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

# model 文件相对本仓库根
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODEL = (
    _REPO_ROOT
    / "third_party/mujoco_menagerie/unitree_go2/scene.xml"
)

# 与 synth/generator.DEFAULT_PARAMS 保持一致的 Go2 电机热参数（一阶热模型）
MOTOR_PARAMS: Dict[str, float] = {
    "kt": 0.25,     # N·m/A 扭矩常数
    "R": 0.35,      # Ohm 绕组电阻
    "Cth": 900.0,   # J/K 电机+外壳热容
    "Rth": 0.55,    # K/W 散热热阻
    "T_amb": 25.0,  # °C 环境温度
}

# 12 个执行器名（顺序固定，与 go2.xml <actuator> 一致）
ACTUATORS = [
    "FL_hip", "FL_thigh", "FL_calf",
    "FR_hip", "FR_thigh", "FR_calf",
    "RL_hip", "RL_thigh", "RL_calf",
    "RR_hip", "RR_thigh", "RR_calf",
]


@dataclass
class SimResult:
    """mujoco 仿真导出的原始结果（尚不包含合成故障/噪声）。"""
    joints: List[str]
    ts: np.ndarray                 # (n_steps,) 秒
    pos: np.ndarray                # (n_steps, n_joint) rad
    vel: np.ndarray                # (n_steps, n_joint) rad/s
    torque: np.ndarray             # (n_steps, n_joint) N·m（执行器力）
    current: np.ndarray            # (n_steps, n_joint) A  （由 torque/kt 推）
    temperature: np.ndarray        # (n_steps, n_joint) °C（一阶热模型）
    base_pos: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))  # 可选，body 位移


def _pd_target(t, joint_name: str, dt: float) -> float:
    """给出该关节的期望角度（受控小幅摆动）。

    幅度刻意调小：站立四足的 calf(膝盖) 因承重本就接近力矩饱和，hip 偏摆几乎不发热；
    只有 thigh(屈髋) 在"有真实负载、且不过饱和"的区间（基线电流个位数安培、有裕量），
    才能让"健康=正常电流、注入故障=异常"被下游 Agent 判出。所以小幅摆动、软增益。
    """
    kind = joint_name.split("_")[-1]  # hip / thigh / calf
    if kind == "hip":
        return 0.03 * np.sin(2.0 * np.pi * 0.5 * t)
    if kind == "thigh":
        return 0.06 * np.sin(2.0 * np.pi * 0.5 * t + 0.5)
    # calf
    return 0.05 * np.sin(2.0 * np.pi * 0.5 * t + 1.0)


def simulate(
    model_path=None,
    duration_s: float = 2.0,
    dt: float = 0.01,
    kp: float = 30.0,
    kd: float = 2.0,
    seed: Optional[int] = None,
) -> SimResult:
    """加载 go2 模型，PD 追踪正弦摆动，导出关节遥测 + 热模型温度。

    参数：
      duration_s  仿真时长（秒）
      dt          采样间隔（秒，默认 100Hz）
      kp / kd     PD 控制增益（让关节跟得上目标）
      seed        mujoco 的随机种子（运动噪声），None 则不可复现
    """
    import mujoco  # 本地导入，允许 mujoco 未安装时其它模块正常 import

    model_path = model_path or DEFAULT_MODEL
    model = mujoco.MjModel.from_xml_path(str(model_path))
    data = mujoco.MjData(model)

    nu = model.nu
    if nu != len(ACTUATORS):
        raise RuntimeError(
            f"模型执行器数量 {nu} 与 ACTUATORS 长度 {len(ACTUATORS)} 不符，"
            f"可能 go2.xml 结构变化（当前只支持 12 电机四足）。"
        )

    # 关节下标：qpos 从 index 7 起对应 12 个关节（前 7 个是基座 freejoint）
    joint_idx = list(range(7, 7 + 12))
    # 执行器名(FL_hip) → 关节名(FL_hip_joint) → 模型关节 id / 速度 dof 下标
    joint_names = [a + "_joint" for a in ACTUATORS]
    jnt_ids = [model.joint(n).id for n in joint_names]
    dof_ids = [model.jnt_dofadr[j] for j in jnt_ids]

    n_steps = int(duration_s / dt)
    pos = np.zeros((n_steps, 12))
    vel = np.zeros((n_steps, 12))
    torque = np.zeros((n_steps, 12))
    current = np.zeros((n_steps, 12))
    temp = np.full((n_steps, 12), MOTOR_PARAMS["T_amb"])
    base_pos = np.zeros((n_steps, 3))
    ts = np.arange(n_steps) * dt

    kt = MOTOR_PARAMS["kt"]
    R = MOTOR_PARAMS["R"]
    Cth = MOTOR_PARAMS["Cth"]
    Rth = MOTOR_PARAMS["Rth"]
    T_amb = MOTOR_PARAMS["T_amb"]
    T = np.full(12, T_amb)

    # 运行初始几步让接触稳定（预滚动）
    for _ in range(500):
        mujoco.mj_step(model, data)

    for i in range(n_steps):
        t = ts[i]
        # 设定目标 + PD 计算力矩
        for a, name in enumerate(ACTUATORS):
            target = _pd_target(t, name, dt)
            q = data.qpos[joint_idx[a]]
            qd = data.qvel[dof_ids[a]]
            tau = kp * (target - q) - kd * qd
            data.ctrl[a] = np.clip(tau, model.actuator_ctrlrange[a][0],
                                   model.actuator_ctrlrange[a][1])

        mujoco.mj_step(model, data)

        # 采集
        pos[i] = data.qpos[joint_idx]
        vel[i] = data.qvel[dof_ids]
        torque[i] = data.ctrl  # 记录下发力矩（近似电机净输出）
        current[i] = np.abs(torque[i]) / kt

        # 一阶热模型更新：基于电流发热
        dT = (current[i] ** 2 * R - (T - T_amb) / Rth) * dt / Cth
        T = T + dT
        temp[i] = T

        base_pos[i] = data.qpos[0:3]

    return SimResult(
        joints=list(ACTUATORS),
        ts=ts,
        pos=pos,
        vel=vel,
        torque=torque,
        current=current,
        temperature=temp,
        base_pos=base_pos,
    )


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="运行 Go2 mujoco 仿真并导出遥测")
    p.add_argument("--dur", type=float, default=2.0, help="仿真时长（秒）")
    p.add_argument("--dt", type=float, default=0.01, help="采样间隔（秒）")
    p.add_argument("--out", type=str, default="", help="输出 .npz 路径（默认不写文件）")
    args = p.parse_args()

    r = simulate(duration_s=args.dur, dt=args.dt)
    print(f"步数 {r.ts.shape[0]}, 关节 {len(r.joints)}")
    print("关节力矩[N·m] 范围:", r.torque.min().round(2), "~", r.torque.max().round(2))
    print("电流[A] 范围:      ", r.current.min().round(2), "~", r.current.max().round(2))
    print("温度[°C] 末值:     ", r.temperature[-1].round(1))
    print("基座位移[m] 末:     ", r.base_pos[-1].round(3))

    if args.out:
        import os
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        np.savez_compressed(
            args.out,
            ts=r.ts, joints=np.array(r.joints),
            pos=r.pos, vel=r.vel, torque=r.torque,
            current=r.current, temperature=r.temperature, base_pos=r.base_pos,
        )
        print("已写出:", args.out)
