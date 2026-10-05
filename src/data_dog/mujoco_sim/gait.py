"""Trot 步态控制器：给 12 个电机生成随时间变化的 PD 目标角。

为什么需要
----------
官方 Go2 模型（`third_party/mujoco_menagerie/unitree_go2/go2.xml`）**只提供一个 `home`
站立 keyframe、12 个 `motor` 力矩直驱执行器，没有任何步态控制器**。要让狗"走路"以产生
有真实负载调制的遥测，必须自己写步态。本模块只做一件事：**给定时间 t，算出 12 个关节的
期望角度**（绝对角），交回给 `simulate.py` 的 PD 环去跟踪。

Trot（小跑）原理
----------------
四足对角步态：**对角两条腿同相**（FL+RR 一组，FR+RL 一组），两组相位差 π。
- 摆动相（swing）：腿抬起（膝更弯）并向前摆 → 足离地。
- 支撑相（stance）：腿伸直并向后蹬 → 足着地、推动机身。

本实现是**开环相位驱动**：不做状态估计、不做平衡反馈。因此**不保证自由基座一定不摔**——
稳定性靠 `simulate.py` 的 PD 增益与步频/幅度调参，并由 `fell` 判据事后标定（见
`SimResult.fell`）。这是诚实边界：开环相位驱动是"尽量还原"的起点，不是收敛保证。

保真度档位（mode）
------------------
- `free`   ：自由基座 trot（最还原，主路径）。狗自己平衡，可能翻倒。
- `suspended`：基座加竖直弱弹簧支撑（由 simulate 施加），腿仍正常摆动受力——
                排除"摔倒"这一失败模式，但机身垂直运动被约束，**非完整运动学**。
- `micro_swing`：退回原有的站立微幅摆动（无步态），仅作最后兜底；负载调制很弱。

每档都必须在外层标保真度标签，**不得把 suspended/micro_swing 冒充成"真走路"**。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# 执行器顺序（与 go2.xml <actuator> 及 simulate.ACTUATORS 完全一致）
ACTUATORS = [
    "FL_hip", "FL_thigh", "FL_calf",
    "FR_hip", "FR_thigh", "FR_calf",
    "RL_hip", "RL_thigh", "RL_calf",
    "RR_hip", "RR_thigh", "RR_calf",
]

LEGS = ["FL", "FR", "RL", "RR"]

# trot 对角对：FL↔RR 同相，FR↔RL 同相（相位差 π）
_PHASE0 = {"FL": 0.0, "RR": 0.0, "FR": np.pi, "RL": np.pi}

# 关节角范围（直接抄 go2.xml，用于 clip；前后腿 thigh 范围不同）
_KNEE_RANGE = (-2.7227, -0.83776)                 # <joint class="knee">
_THIGH_RANGE = {                                   # 前/后髋抬腿关节 range 不同
    "FL": (-1.5708, 3.4907), "FR": (-1.5708, 3.4907),   # front_hip
    "RL": (-0.5236, 4.5379), "RR": (-0.5236, 4.5379),   # back_hip
}
_HIP_RANGE = (-1.0472, 1.0472)                     # abduction

# home 站立姿态（go2.xml keyframe "home"）：thigh=0.9, knee=-1.8，hip=0
HOME_THIGH = 0.9
HOME_KNEE = -1.8
HOME_HIP = 0.0


@dataclass(frozen=True)
class GaitParams:
    """步态参数。默认值均为**先验初值 / 待调**（见模块头诚实边界）。

    `stroke`/`lift`/`freq` 的默认值是经 5s 自由基座试跑标定的**可用起点**：
    约 0.05 m/s 净前进、up_z≈0.999 不翻倒（见 scripts/mj_smoke.py）。
    """
    freq: float = 1.0          # 步频 Hz；0 表示不迈步（站立）
    a_hip: float = 0.02        # 髋侧摆幅度 rad
    stroke: float = 0.3        # 大腿前后扫掠幅度 rad（peak-to-peak / 2）
    lift: float = 0.3          # 摆动相抬膝幅度 rad（负向，膝更弯）
    duty: float = 0.5          # 支撑相占比（trot 取 0.5）
    mode: str = "free"         # free | suspended | micro_swing
    kp_hip: float = 40.0
    kp_thigh: float = 40.0
    kp_knee: float = 60.0
    kd_hip: float = 2.0
    kd_thigh: float = 2.0
    kd_knee: float = 3.0


def _clip(v: float, lo: float, hi: float) -> float:
    return float(min(max(v, lo), hi))


def joint_targets(t: float, p: GaitParams) -> np.ndarray:
    """给定时间 t（秒），返回 12 个执行器的目标角（顺序 = ACTUATORS）。"""
    out = np.zeros(12)
    for leg in LEGS:
        phi = 2.0 * np.pi * p.freq * t + _PHASE0[leg]

        if p.mode == "micro_swing":
            # 退回站立微摆：与旧 simulate._pd_target 口径**逐字一致**（保回归）。
            # 注意 knee 目标是绝对角 ≈0（不是 home 的 -1.8）：旧代码故意把膝 target
            # 设到 ~0，令 PD 持续大力矩顶伸承重 → 膝电流/发热高，热故障才可辨。
            # 且**不套关节 range 裁剪**（旧行为如此；range 裁剪留给 trot 档）。
            out[LEGS.index(leg) * 3 + 0] = 0.03 * np.sin(2.0 * np.pi * 0.5 * t)
            out[LEGS.index(leg) * 3 + 1] = 0.06 * np.sin(2.0 * np.pi * 0.5 * t + 0.5)
            out[LEGS.index(leg) * 3 + 2] = 0.05 * np.sin(2.0 * np.pi * 0.5 * t + 1.0)
            continue
        else:
            # trot 相位驱动（线性扫掠，可产生净前进）。
            # 用归一化相位 s∈[0,1)：s<duty 支撑相（线性后扫、膝伸直承重）；
            # s>=duty 摆动相（线性前摆 + 正弦抬膝）。**支撑相净位移 > 摆动相回摆**，
            # 故每周期有净前进（对称正弦步态的净位移恒为 0，测过——不采用）。
            hip = HOME_HIP + p.a_hip * np.sin(phi + np.pi / 2)   # 侧向微摆
            s = (phi / (2.0 * np.pi)) % 1.0
            if s < p.duty:
                frac = s / p.duty
                thigh = HOME_THIGH + p.stroke * (0.5 - frac)     # 前(+0.5) → 后(-0.5)
                knee = HOME_KNEE
            else:
                frac = (s - p.duty) / (1.0 - p.duty)
                thigh = HOME_THIGH + p.stroke * (-0.5 + frac)    # 后 → 前（快速回摆）
                knee = HOME_KNEE - p.lift * np.sin(np.pi * frac) # 抬起跨过地面

        tlo, thi = _THIGH_RANGE[leg]
        out[LEGS.index(leg) * 3 + 0] = _clip(hip, *_HIP_RANGE)
        out[LEGS.index(leg) * 3 + 1] = _clip(thigh, tlo, thi)
        out[LEGS.index(leg) * 3 + 2] = _clip(knee, *_KNEE_RANGE)
    return out


def gains(p: GaitParams) -> tuple[np.ndarray, np.ndarray]:
    """返回 (kp[12], kd[12])，顺序 = ACTUATORS，供 PD 环用。"""
    kp = np.zeros(12)
    kd = np.zeros(12)
    for i, name in enumerate(ACTUATORS):
        kind = name.split("_")[-1]
        kp[i] = {"hip": p.kp_hip, "thigh": p.kp_thigh, "calf": p.kp_knee}[kind]
        kd[i] = {"hip": p.kd_hip, "thigh": p.kd_thigh, "calf": p.kd_knee}[kind]
    return kp, kd


def is_fallen(base_pos: np.ndarray, base_quat: np.ndarray,
              min_z: float = 0.20, min_up_z: float = 0.7) -> bool:
    """是否翻倒：基座最低高度 < min_z，或机体 up 向量 z 分量 < min_up_z（倾斜 > ~45°）。

    base_pos: (n,3)，base_quat: (n,4) MuJoCo 序 (w,x,y,z)。
    机身 up 向量 = 旋转矩阵对世界 z 轴的像 = (2(xz - wy), 2(yz + wx), 1-2(x²+y²))。
    """
    if base_pos.size == 0 or np.isnan(base_pos).any():
        return True
    up_z = 1.0 - 2.0 * (base_quat[:, 1] ** 2 + base_quat[:, 2] ** 2)
    return bool(base_pos[:, 2].min() < min_z or up_z.min() < min_up_z)


__all__ = [
    "ACTUATORS", "LEGS", "GaitParams",
    "joint_targets", "gains", "is_fallen",
    "HOME_HIP", "HOME_THIGH", "HOME_KNEE",
]
