"""关节以外的子系统遥测：电池、IMU、热管理、通信、整机。

为什么需要这一层
----------------
MuJoCo 只算刚体动力学。真机上报的可观测通道远不止 12 个关节：
`vendor_docs/unitree/cleaned/raw/` 下的 IDL 里，还有 BmsState / IMUState /
LowState / SportModeState。只做关节，就只能演 5 类电机故障；把这几条通道补上，
才能覆盖"机器狗会出现的所有故障"。

诚实边界（必须明说）
--------------------
- **电池**：不是电化学模型。用"内阻 + 容量"做等效电路：
  端电压 = 开路电压(由 SOC 查表) − I·R_int。这能正确复现"负载加重 → 母线
  电压下垂 → 恒功率下电流抬升"这条真实因果链，也是区分电池故障与电机故障
  的关键。但它不会告诉你电芯内部发生了什么。
- **IMU**：真值来自 MuJoCo 的机身姿态（这是真物理），陀螺/加速度计是在真值
  上叠加噪声与零偏得到的读数。
- **风扇/通信/整机**：没有物理模型，是**信号级**模型——按真实设备的行为
  特征生成读数（风扇转速、CRC 失败率、error_code）。
- 这些参数的取值凡是厂商文档给了的引用出处，没给的显式标为项目假设。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------- 电池参数
# Go2 电池包为 6S 锂电池组（21.6V 标称 / 25.2V 满充）。厂商手册只说"智能电池"，
# 未给电芯参数，故下列数值为**项目假设**，采用消费级 6S 147Wh 量级的常见值。
#
# ⚠ 与既有数据集的不一致（必须知道，不要当成笔误）：
#   `data/mj_10k/` 里的 10 万条数据用 **48V 母线**（`v_bus_oc` 40/44/48/52V，
#   见 `mujoco_sim/conditions.py`），那是把 Go2 当成 48V 平台的假设。
#   真实 Go2 是 21.6V 6S（147Wh ≈ 6.8Ah），本模块按**真实硬件**建模。
#   两者不能混用：跨源比较 v_bus 这一类绝对量会差一倍。
#   做"新旧数据对照"时，只比无量纲量（温升、比值、趋势），不比电压绝对值。
BATTERY = {
    "cells": 6,
    "nominal_v": 21.6,          # 6 × 3.6V
    "full_v": 25.2,             # 6 × 4.2V
    "empty_v": 19.8,            # 6 × 3.3V，再低进入保护
    "capacity_ah": 6.8,         # 147Wh / 21.6V ≈ 6.8Ah
    "r_int": 0.055,             # Ω，整包等效内阻（新电池）
    "r_int_worn": 0.14,         # Ω，老化后内阻（约为新的 2.5 倍）
    "soc_warn": 20.0,           # %，厂商 LED"低电量 10 分钟内趴下"对应的量级
    "temp_warn": 55.0,          # °C，电池包 NTC 告警（项目假设）
    "temp_crit": 65.0,
    "cell_imbalance_warn": 0.050,   # V，单体极差告警（项目假设）
    # 持续放电限值（项目假设）：Go2 整机持续功率在数百瓦量级，
    # 21.6V 下对应十几安；取 30A 作为持续放电上限，超过才置过流保护位。
    # **不要用 "容量×倍率" 反推**——那会把常态负载误判成过流。
    "current_crit": 30.0,
    # 电池包热模型（项目假设，取值依据见下）：
    #   6S 6.8Ah 电池包约 1.1kg，等效比热 ~1000 J/(kg·K) → Cth ≈ 1100 J/K
    #   带外壳自然对流+密封结构，Rth ≈ 2.5 K/W
    #   → 时间常数 Cth·Rth ≈ 2750s ≈ 46min（电池升降温本就比电机慢得多）
    # 校验：3A 稳态 ΔT≈1.2K（26°C）；10A 稳态 ΔT≈14K（39°C）；
    #       20A 稳态 ΔT≈55K（80°C，越过 65°C 保护线）——与工程直觉一致。
    "Cth_j_per_k": 1100.0,
    "Rth_k_per_w": 2.5,
}


@dataclass
class BatteryState:
    """一次 BMS 读数（字段名对齐 IDL BmsState）。"""

    soc: float = 100.0                 # %
    voltage: float = BATTERY["full_v"]  # V，端电压（power_v）
    current: float = 0.0               # A，放电为正
    power_w: float = 0.0
    cell_vol: list[float] = field(default_factory=list)
    bq_ntc: float = 25.0               # °C，电池包温度
    mcu_ntc: float = 25.0
    cycle: int = 0
    status: int = 0

    def as_metrics(self) -> dict[str, float]:
        """转成扁平指标（给信封 payload / UI 用）。"""
        out = {
            "soc": round(float(self.soc), 2),
            "power_v": round(float(self.voltage), 3),
            "current": round(float(self.current), 3),
            "power_a": round(float(self.current), 3),
            "bq_ntc": round(float(self.bq_ntc), 2),
            "mcu_ntc": round(float(self.mcu_ntc), 2),
            "cycle": float(self.cycle),
            "status": float(self.status),
        }
        if self.cell_vol:
            arr = np.asarray(self.cell_vol)
            out["cell_v_max"] = round(float(arr.max()), 3)
            out["cell_v_min"] = round(float(arr.min()), 3)
            out["cell_v_spread"] = round(float(arr.max() - arr.min()), 4)
        return out


class BatteryModel:
    """电池等效电路 + 容量积分。

    端电压 = 开路电压(SOC) − I·R_int。这条式子虽简单，却如实复现了：
    负载增大 → 电流上升 → 端电压下垂 → 恒功率下电流进一步上升。
    """

    def __init__(self, *, seed: int = 0, worn: bool = False) -> None:
        p = BATTERY
        self.n_cells = int(p["cells"])
        self.cap_ah = float(p["capacity_ah"])
        self.r_int = float(p["r_int_worn"] if worn else p["r_int"])
        self.soc = 100.0
        self.cycle = 0
        self._cells = np.full(self.n_cells, p["full_v"] / self.n_cells)
        self._rng = np.random.default_rng(seed)
        # 电池包热模型：比电机慢得多，热容大一个量级（项目假设）
        self._T = 25.0
        # 最近一次读数，供调用方查当前电压（估母线电流用）
        self.state = BatteryState()

    def _ocv(self, soc: float) -> float:
        """开路电压查表：SOC 100%→满充，0%→空。中间近似线性带轻微平台。"""
        p = BATTERY
        s = max(0.0, min(100.0, soc)) / 100.0
        # 锂电池 OCV 曲线在两端较陡、中间较平，用平滑的 S 形近似
        f = 0.5 - 0.5 * np.cos(np.pi * s)      # 0→0, 0.5→0.5, 1→1
        return p["empty_v"] + (p["full_v"] - p["empty_v"]) * f

    def step(self, *, total_current: float, dt: float, load_extra: float = 1.0,
             voltage_sag: float = 0.0, imbalance: float = 0.0,
             overcurrent: bool = False, soc_override: float | None = None,
             thermal_offset: float = 0.0,
             thermal_scale: float = 1.0) -> BatteryState:
        """按本步总电流推进电池状态。

        total_current ：**整机母线电流**（A）。由调用方按"机械功率 ÷ 效率 ÷ 电压"
                        折算。**不要传 12 路关节电流之和**——那是各关节相电流的
                        累加，不是母线上流过的电流，量级差一个数量级。
        load_extra    ：额外负载系数（F5 / F15 用）
        voltage_sag   ：额外压降 V（F14 母线电压跌落用）
        imbalance     ：单体不均衡幅度 V（F12 用）
        soc_override  ：直接指定 SOC（F11 用——对应"换上一块快没电的电池"）
        thermal_offset：包内热平衡温度的偏移 K（F13 用——散热不良时平衡点被抬高）
        thermal_scale ：热时间缩放，与电机共用同一个配置（见 SimConfig）
        """
        if soc_override is not None:
            self.soc = float(max(0.0, min(100.0, soc_override)))

        i_total = max(0.0, total_current) * load_extra
        ocv = self._ocv(self.soc)
        v_terminal = ocv - i_total * self.r_int - voltage_sag
        v_terminal = max(BATTERY["empty_v"] * 0.85, v_terminal)

        # 容量积分（放电为负）
        d_soc = -(i_total * dt) / (self.cap_ah * 3600.0) * 100.0
        self.soc = float(max(0.0, min(100.0, self.soc + d_soc)))

        # 单体电压：均分端子电压 + 不均衡扰动
        per = v_terminal / self.n_cells
        jitter = self._rng.normal(0.0, 0.004, self.n_cells)
        if imbalance > 0.0:
            # 只让最差那一节偏，模拟单节衰减
            jitter = jitter - np.linspace(0, imbalance, self.n_cells)
        self._cells = per + jitter

        # 电池温度：与电机同构的一阶模型 —— I²R 产热，经 Rth 向环境散热。
        # dT = (I²R − (T−T_amb)/Rth)·dt / Cth，稳态 ΔT = I²R·Rth。
        heat = i_total ** 2 * self.r_int                 # W
        t_amb = 25.0 + thermal_offset
        cth = BATTERY["Cth_j_per_k"] / max(0.05, thermal_scale)
        self._T += (heat - (self._T - t_amb) / BATTERY["Rth_k_per_w"]) * dt / cth

        status = 0
        if overcurrent or i_total > BATTERY["current_crit"]:
            status |= 0x01                                     # 过流保护位
        if self._T >= BATTERY["temp_crit"]:
            status |= 0x02                                     # 过温保护位
        if self.soc <= 1.0:
            status |= 0x04                                     # 低电量保护位

        self.state = BatteryState(
            soc=self.soc, voltage=float(v_terminal), current=float(i_total),
            power_w=float(v_terminal * i_total),
            cell_vol=[float(v) for v in self._cells],
            bq_ntc=float(self._T), mcu_ntc=float(self._T + 1.5),
            cycle=self.cycle, status=status,
        )
        return self.state


# ---------------------------------------------------------------- IMU
@dataclass
class ImuState:
    """一次 IMU 读数（字段名对齐 IDL IMUState）。"""

    quat: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0)
    gyro: tuple[float, float, float] = (0.0, 0.0, 0.0)      # rad/s
    accel: tuple[float, float, float] = (0.0, 0.0, -9.81)    # m/s²
    rpy: tuple[float, float, float] = (0.0, 0.0, 0.0)        # rad
    temperature: float = 30.0

    def as_metrics(self) -> dict[str, float]:
        roll, pitch, yaw = self.rpy
        gx, gy, gz = self.gyro
        ax, ay, az = self.accel
        qw, qx, qy, qz = self.quat
        return {
            # IDL `IMUState.quaternion`（array[float32,4]，w,x,y,z）。
            # **不是** rpy 的另一种写法：欧拉角在俯仰 ±90° 处奇异，四元数不奇异，
            # 而且真机上姿态融合出来的就是四元数——下游做重力对齐要用它。
            # 之前只发 rpy、把 `self.quat` 白存着，是纯粹的漏字段。
            "quat_w": round(float(qw), 6), "quat_x": round(float(qx), 6),
            "quat_y": round(float(qy), 6), "quat_z": round(float(qz), 6),
            "roll": round(float(roll), 4), "pitch": round(float(pitch), 4),
            "yaw": round(float(yaw), 4),
            "gyro_x": round(float(gx), 5), "gyro_y": round(float(gy), 5),
            "gyro_z": round(float(gz), 5),
            "accel_x": round(float(ax), 4), "accel_y": round(float(ay), 4),
            "accel_z": round(float(az), 4),
            "accel_norm": round(float(np.linalg.norm(self.accel)), 4),
            "imu_temp": round(float(self.temperature), 2),
        }


def quat_to_rpy(q: tuple[float, float, float, float]) -> tuple[float, float, float]:
    """MuJoCo 四元数 (w,x,y,z) → (roll, pitch, yaw)。"""
    w, x, y, z = q
    roll = float(np.arctan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)))
    sinp = 2 * (w * y - z * x)
    pitch = float(np.arcsin(max(-1.0, min(1.0, sinp))))
    yaw = float(np.arctan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))
    return roll, pitch, yaw


class ImuModel:
    """在 MuJoCo 真值上叠加噪声与零偏，产出 IMU 读数。

    真值来自机身姿态（真物理）；读数才是传感器看到的东西。
    """

    def __init__(self, *, seed: int = 0) -> None:
        self._rng = np.random.default_rng(seed)
        self._T = 30.0

    def step(self, *, quat, gyro_true, accel_true, dt: float,
             gyro_bias: float = 0.0, accel_fail: bool = False,
             extra_temp: float = 0.0) -> ImuState:
        rpy = quat_to_rpy(tuple(quat))
        g = np.asarray(gyro_true, dtype=float) + self._rng.normal(0, 0.002, 3)
        a = np.asarray(accel_true, dtype=float) + self._rng.normal(0, 0.03, 3)
        if gyro_bias > 0.0:
            # 零偏：静止时也读数非零，积分后姿态会漂
            g = g + np.array([gyro_bias, gyro_bias * 0.6, 0.0])
        if accel_fail:
            # 加速度计失效：模长偏离 1g（如灵敏度掉了 30%）
            a = a * 0.70
        self._T += (25.0 + extra_temp - self._T) * dt / 120.0
        return ImuState(
            quat=tuple(float(v) for v in quat),
            gyro=tuple(float(v) for v in g),
            accel=tuple(float(v) for v in a),
            rpy=rpy, temperature=float(self._T),
        )


# ---------------------------------------------------------------- 热管理
@dataclass
class ThermalState:
    """散热与通信类读数（来自 LowState 的若干字段）。"""

    fan_frequency: float = 1.0      # 归一化 0..1，1 = 满转
    bandwidth: int = 1000            # 总线带宽计数（设备自定义量纲）
    crc_fail_rate: float = 0.0       # 0..1
    temperature_ntc1: float = 30.0

    def as_metrics(self) -> dict[str, float]:
        return {
            "fan_frequency": round(float(self.fan_frequency), 3),
            "bandwidth": float(self.bandwidth),
            "crc_fail_rate": round(float(self.crc_fail_rate), 4),
            "temperature_ntc1": round(float(self.temperature_ntc1), 2),
        }


def fan_frequency(*, temp_max: float, stalled: bool = False) -> float:
    """风扇转速：温控曲线。停转故障直接给 0。"""
    if stalled:
        return 0.0
    # 40°C 以下怠速 30%，70°C 以上满转
    frac = max(0.0, min(1.0, (temp_max - 40.0) / 30.0))
    return 0.30 + 0.70 * frac


# ---------------------------------------------------------------- 整机
@dataclass
class SystemState:
    """整机/步态读数（字段名对齐 IDL SportModeState）。

    IDL 里 SportModeState 共有 error_code / mode / progress / gait_type /
    foot_raise_height / position / body_height / velocity / yaw_speed /
    range_obstacle / foot_force / foot_position_body / foot_speed_body /
    path_point。这里携带其中**能判故障**的那些：

    - `foot_speed_body` 是判定足端打滑（F22）的唯一依据——着地腿的足端
      速度应该≈0，打滑时它非零。只看 foot_force 是看不出来的（力还在）。
    - `progress` 是步态相位，配合 `gait_type` 才能判"声明与实际不符"（F21）。
    - `range_obstacle` / `path_point` 属于导航规划，与故障诊断无关，未纳入。
    """

    error_code: int = 0
    gait_type: int = 0              # 0=idle 1=trot 2=pace ...
    body_height: float = 0.27
    foot_force: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    mode: int = 0
    progress: float = 0.0                                   # 步态相位 0..1
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)   # 世界系
    velocity: tuple[float, float, float] = (0.0, 0.0, 0.0)   # m/s
    yaw_speed: float = 0.0
    foot_raise_height: float = 0.0
    # 4 条腿的足端速度模长（m/s）。着地时应≈0，>0 即在打滑。
    foot_speed_body: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def as_metrics(self) -> dict[str, float]:
        f = self.foot_force
        v = self.velocity
        s = self.foot_speed_body
        return {
            "error_code": float(self.error_code),
            "gait_type": float(self.gait_type),
            "body_height": round(float(self.body_height), 4),
            "foot_force_fl": round(float(f[0]), 2),
            "foot_force_fr": round(float(f[1]), 2),
            "foot_force_rl": round(float(f[2]), 2),
            "foot_force_rr": round(float(f[3]), 2),
            "sport_mode": float(self.mode),
            "progress": round(float(self.progress), 4),
            "pos_x": round(float(self.position[0]), 4),
            "pos_y": round(float(self.position[1]), 4),
            "vel_x": round(float(v[0]), 4),
            "vel_y": round(float(v[1]), 4),
            "vel_z": round(float(v[2]), 4),
            "speed": round(float(np.hypot(v[0], v[1])), 4),
            "yaw_speed": round(float(self.yaw_speed), 4),
            "foot_raise_height": round(float(self.foot_raise_height), 4),
            # 打滑指标：着地足的足端速度最大值。0 = 抓地，>0 = 在滑。
            "foot_slip_max": round(float(max(s)), 4),
            "foot_speed_fl": round(float(s[0]), 4),
            "foot_speed_fr": round(float(s[1]), 4),
            "foot_speed_rl": round(float(s[2]), 4),
            "foot_speed_rr": round(float(s[3]), 4),
        }


__all__ = [
    "BATTERY", "BatteryState", "BatteryModel",
    "ImuState", "ImuModel", "quat_to_rpy",
    "ThermalState", "fan_frequency",
    "SystemState",
]
