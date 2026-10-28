"""Go2 姿态定义：站立 / 奔跑 / 趴卧，以及各自的关节目标角。

为什么姿态要单独定义
--------------------
不同姿态下同一台机器的"正常"完全不同：站立时膝关节持续承重、电流基线高；
趴卧时腿折在身下、大部分关节几乎不受力；奔跑时电流随步频周期性大幅波动。
同一套故障判据套到不同姿态上会误判——所以姿态必须显式建模、显式标注，
而不是让下游 Agent 去猜。

三个参数都是**真实关节限位**内的值（限位取自 `third_party/.../go2.xml`）：
  hip   ∈ [-1.047, 1.047] rad   （外摆）
  thigh ∈ [-1.571, 3.491] rad   （前腿；后腿下限 -0.5236）
  calf  ∈ [-2.723, -0.838] rad  （膝，恒为负）

诚实边界
--------
本模块只给**关节角目标**，姿态能否稳住由 `rig.py` 的 PD 环与物理接触决定。
趴卧姿态的"翻倒"判据不能用站立那条 `base_z < 0.20`——趴着本来就低，
所以每个姿态自带 `min_z`（见 `Posture.fallen_min_z`）。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

LEGS = ["FL", "FR", "RL", "RR"]

# 关节限位（抄自 go2.xml，用于生成目标角后裁剪；前/后腿 thigh 下限不同）
HIP_RANGE = (-1.0472, 1.0472)
THIGH_RANGE_FRONT = (-1.5708, 3.4907)
THIGH_RANGE_BACK = (-0.5236, 4.5379)
CALF_RANGE = (-2.7227, -0.83776)


@dataclass(frozen=True)
class Posture:
    """一种姿态：稳态关节角 + 步态参数 + 该姿态下的翻倒判据。"""

    key: str
    label: str
    # 稳态目标角（支撑/站立时的目标）
    hip: float
    thigh: float
    calf: float
    # 步态：freq=0 表示不迈步
    gait_freq: float = 0.0
    stroke: float = 0.0      # 大腿前后扫掠幅度（trot 用）
    lift: float = 0.0        # 摆动相抬膝幅度
    a_hip: float = 0.0       # 髋侧摆幅度
    # 该姿态视为"正常"的机身高度区间，以及翻倒判据
    nominal_z: float = 0.27
    fallen_min_z: float = 0.20
    # 描述的物理负载特征（给下游/UI 用，不影响仿真）
    load_note: str = ""

    def targets(self, t: float) -> np.ndarray:
        """给定时刻 t，返回 12 个执行器目标角（顺序 FL,FR,RL,RR × hip,thigh,calf）。"""
        out = np.zeros(12)
        for i, leg in enumerate(LEGS):
            if self.gait_freq > 0.0:
                hip, thigh, calf = self._trot(t, leg)
            else:
                hip, thigh, calf = self.hip, self.thigh, self.calf
            thi_lo = THIGH_RANGE_FRONT[0] if leg in ("FL", "FR") else THIGH_RANGE_BACK[0]
            thi_hi = THIGH_RANGE_FRONT[1] if leg in ("FL", "FR") else THIGH_RANGE_BACK[1]
            out[i * 3 + 0] = float(np.clip(hip, *HIP_RANGE))
            out[i * 3 + 1] = float(np.clip(thigh, thi_lo, thi_hi))
            out[i * 3 + 2] = float(np.clip(calf, *CALF_RANGE))
        return out

    def _trot(self, t: float, leg: str) -> tuple[float, float, float]:
        """trot 相位驱动：对角腿同相（FL↔RR / FR↔RL），支撑相线性后扫、摆动相前摆抬膝。

        与 `mujoco_sim.gait.joint_targets` 同一套相位逻辑，但目标是**本姿态**
        的稳态角而非写死的 home，这样站立/趴卧/奔跑可以共用同一段代码。
        """
        phase0 = {"FL": 0.0, "RR": 0.0, "FR": np.pi, "RL": np.pi}[leg]
        phi = 2.0 * np.pi * self.gait_freq * t + phase0
        hip = self.hip + self.a_hip * np.sin(phi + np.pi / 2)
        s = (phi / (2.0 * np.pi)) % 1.0
        if s < 0.5:                                   # 支撑相：前 → 后，膝伸直承重
            frac = s / 0.5
            thigh = self.thigh + self.stroke * (0.5 - frac)
            calf = self.calf
        else:                                          # 摆动相：后 → 前，抬膝
            frac = (s - 0.5) / 0.5
            thigh = self.thigh + self.stroke * (-0.5 + frac)
            calf = self.calf - self.lift * np.sin(np.pi * frac)
        return hip, thigh, calf


# 三个姿态的取值依据见模块头：全部落在真实关节限位内。
POSTURES: dict[str, Posture] = {
    "stand": Posture(
        key="stand", label="站立", hip=0.0, thigh=0.9, calf=-1.8,
        nominal_z=0.27, fallen_min_z=0.20,
        load_note="四腿支撑，膝持续承重，电流基线平稳",
    ),
    "run": Posture(
        key="run", label="奔跑", hip=0.0, thigh=0.9, calf=-1.8,
        # stroke **必须为负**：正 stroke 会把机体往后推（实测 −0.053 m/s，
        # Δx=−0.607m/10s）。本模型的 thigh 关节正方向使"后扫"对应正角度，
        # 所以前进要取负。原写法 stroke=+0.42 实际是"原地小步后退"，
        # 与"奔跑"这个标签矛盾，录像里会明显看出来。
        #
        # 取值依据（实测扫描，60s 稳定不翻）：
        #   stroke=-1.5 freq=2.2 lift=0.5 → vx=+0.40 m/s 恒定，
        #   z 稳定在 0.213~0.214m，60s 内 0 次翻倒、位移 24.1m。
        #   再大（-1.8）会出现周期性跌到 z=0.058m 的失稳。
        # 0.40 m/s 比真机 Go2 trot（约 1 m/s）慢，这是本简化模型
        # 用纯 PD + 相位驱动、没有全身 MPC/足端力控的固有差距。
        gait_freq=2.2, stroke=-1.5, lift=0.5, a_hip=0.02,
        nominal_z=0.25, fallen_min_z=0.18,
        load_note="trot 对角步态，电流随步频周期波动、峰值远高于站立",
    ),
    "prone": Posture(
        key="prone", label="趴卧", hip=0.0, thigh=1.6, calf=-2.55,
        nominal_z=0.11, fallen_min_z=0.05,
        load_note="腿折于身下，机身靠地面支撑，多数关节近乎不受力",
    ),
}

__all__ = ["POSTURES", "Posture", "LEGS"]
