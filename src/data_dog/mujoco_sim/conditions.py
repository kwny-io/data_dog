"""试验条件网格 + 分层采样：定义"一条 trial"是什么，以及百万条怎么分布。

一条 trial = 一段仿真（TrialConfig），产出一条带 ground truth 的 SynthSample。

为什么不枚举全因子
------------------
7 个维度全因子会爆炸（数亿组合），且大量组合无意义。改用**分层随机采样**：
对每个维度按预设的取值/区间随机取值，但**强制**关键类别（故障类型、normal 层）
达到目标占比，使样本在各层上的分布可控、可复现（给定 seed）。

关键设计：**normal（无故障）占比 ≥30%**——否则无法评估"误报率"（Agent 把健康设备
误判为故障）。同理，故障严重度里刻意包含**低于检测阈值**的档，制造"检测不到"的真实
负例，避免评估变成"阈值背诵"。

诚实边界
--------
所有取值区间都是**工程假设**（payload/坡度/环温/电压/磨损），不是对某台真机的标定；
参数集中在此处，日后可用真机数据替换。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

from .gait import GaitParams
from .noise import NoiseConfig

# 5 类故障 + normal
FAULTS = ["F1_stall", "F2_temp_rise", "F3_packet_loss", "F4_current_bias", "F5_load_spike"]

# 故障可作用的部件（与 Agent 白名单一致；见 to_sample 的映射）
FAULT_COMPONENTS = [
    "joint_motor.front_left_knee",
    "joint_motor.front_right_knee",
    "joint_motor.front_left_hip",
    "joint_motor.front_right_hip",
]


@dataclass(frozen=True)
class TrialConfig:
    """一条试验的完整配置（可哈希、可复现）。"""
    trial_id: int
    seed: int
    # 步态 / 工况
    freq: float                 # 步频 Hz（0=站立）
    payload_kg: float
    slope_deg: float
    t_amb: float                # 环境温度 °C
    v_bus_oc: float             # 开路母线电压 V
    # 磨损（乘子，作用到摩擦/库仑/热阻）
    wear_friction: float
    wear_coulomb: float
    wear_rth: float
    # 故障
    fault: str | None           # None=normal
    severity: float             # 0..1
    component: str
    # 保真度档
    gait_mode: str              # free | suspended | micro_swing
    duration_s: float
    dt: float

    def gait(self) -> GaitParams:
        return GaitParams(freq=self.freq, mode=self.gait_mode)

    def label(self) -> dict | None:
        """ground truth（与 to_sample 的 gt 结构一致，供评估用）。"""
        if not self.fault:
            return None
        from data_dog.synth.generator import FAULT_DEFS
        return {
            "sample_id": self.fault,
            "component": self.component,
            "fault": self.fault,
            "severity": FAULT_DEFS[self.fault]["severity"],
            "window": "00:00~00:01",   # 由 to_sample 覆写为真实窗
            "desc": FAULT_DEFS[self.fault]["desc"],
        }


@dataclass
class SamplingSpec:
    """采样规格：各维度的取值分布 + 目标占比。"""
    duration_s: float = 2.0
    dt: float = 0.01
    normal_frac: float = 0.35            # normal 目标占比（测误报）
    gait_mode: str = "free"              # 默认最还原档
    severities: tuple[float, ...] = (0.5, 0.8, 1.0)  # 含亚阈值档
    fault_weight: dict[str, float] = field(default_factory=lambda: {
        f: 1.0 for f in FAULTS
    })


def _sample_condition(rng: np.random.Generator) -> dict:
    """随机抽一组工况（与故障无关的维度）。"""
    return {
        "freq": float(rng.choice([0.0, 0.5, 1.0, 1.5, 2.0])),
        "payload_kg": float(rng.choice([0.0, 3.0, 8.0, 15.0])),
        "slope_deg": float(rng.choice([0.0, -5.0, 5.0, -10.0, 10.0])),
        "t_amb": float(rng.choice([15.0, 25.0, 35.0, 45.0])),
        "v_bus_oc": float(rng.choice([52.0, 48.0, 44.0, 40.0])),
        "wear_friction": float(rng.choice([1.0, 1.3, 1.8])),
        "wear_coulomb": float(rng.choice([1.0, 1.5, 2.0])),
        "wear_rth": float(rng.choice([1.0, 1.2, 1.4])),
    }


def make_configs(n: int, spec: SamplingSpec, seed: int = 0) -> list[TrialConfig]:
    """确定性生成 n 条 TrialConfig，normal 占比逼近 spec.normal_frac。"""
    rng = np.random.default_rng(seed)
    faults_pool = list(spec.fault_weight.keys())
    weights = np.array([spec.fault_weight[f] for f in faults_pool], dtype=float)
    weights = weights / weights.sum()

    configs: list[TrialConfig] = []
    for i in range(n):
        cond = _sample_condition(rng)
        is_normal = rng.random() < spec.normal_frac
        if is_normal:
            fault, severity, component = None, 0.0, FAULT_COMPONENTS[0]
        else:
            fault = str(rng.choice(faults_pool, p=weights))
            severity = float(rng.choice(spec.severities))
            component = str(rng.choice(FAULT_COMPONENTS))
        configs.append(TrialConfig(
            trial_id=i, seed=int(rng.integers(0, 2**31 - 1)),
            fault=fault, severity=severity, component=component,
            gait_mode=spec.gait_mode, duration_s=spec.duration_s, dt=spec.dt,
            **cond,
        ))
    return configs


def config_to_dict(cfg: TrialConfig) -> dict:
    return asdict(cfg)


def config_from_dict(d: dict) -> TrialConfig:
    return TrialConfig(**d)


__all__ = [
    "FAULTS", "FAULT_COMPONENTS", "TrialConfig", "SamplingSpec",
    "make_configs", "config_to_dict", "config_from_dict",
]
