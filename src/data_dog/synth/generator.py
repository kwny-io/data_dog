"""Go2 合成遥测生成器核心：物理模型 + 故障注入 + ground truth。

纯 NumPy 实现，无 MuJoCo 依赖。接口与"未来 MuJoCo 数据源"对齐：
输出固定采样率、N 个关节、N 秒的遥测流 + 每样本 one 段 ground truth。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# ---- Go2 公开参数（近似，量级来自 Unitree Go2 官方资料）----
DEFAULT_PARAMS: dict[str, float] = {
    # 关节电机座参数：扭矩常数 / 电阻 / 热容 / 热阻（一阶热模型）
    "kt": 0.25,            # N·m/A，扭矩常数
    "R": 0.35,             # Ohm，绕组电阻
    "Cth": 900.0,          # J/K，电机+外壳热容
    "Rth": 0.55,           # K/W，对环境的散热热阻
    "T_amb": 25.0,         # °C，环境温度
    # 动力学
    "joint_damping": 0.75, # N·m·s/rad，关节阻尼（官方量级）
    "coulomb_friction": 0.6,  # N·m，库仑摩擦
    "stall_torque": 23.7,  # N·m，最大堵转扭矩（Go2 hip/knee 量级）
    "no_load_current": 0.8,# A，空载/轻载电流基线
    # 采样
    "dt": 0.01,            # s 采样间隔（100Hz）
}

# 5 类故障定义：名称 + 默认严重度 + 说明（用于 ground truth）
FAULT_DEFS: dict[str, dict[str, str]] = {
    "F1_stall":       {"severity": "critical", "desc": "卡死/堵转，扭矩电流骤升到限幅"},
    "F2_temp_rise":   {"severity": "warning",  "desc": "热退化，温度爬升越阈值"},
    "F3_packet_loss": {"severity": "warning",  "desc": "通信丢包，观测随机缺失"},
    "F4_current_bias":{"severity": "warning",  "desc": "电流传感器零漂，整体偏置"},
    "F5_load_spike":  {"severity": "warning",  "desc": "外部大负载骤增，扭矩电流上探"},
}

# 温度告警阈值（与现有 mock 剧本一致：75°C 热退化 / 90°C 停机）
TEMP_HI_WARN = 75.0
TEMP_HI_CRIT = 90.0


@dataclass
class SynthGenConfig:
    """一次生成的配置。"""
    n_joints: int = 4                       # 生成几个关节
    joints: list[str] = field(default_factory=list)  # 部件名（默认自动命名）
    n_seconds: float = 120.0                # 时长（秒）
    dt: float = 0.01                        # 采样间隔
    seed: int = 0                           # 随机种子
    noise_scale: float = 0.02               # 观测高斯噪声相对幅度
    base_load_torque: float = 3.0           # 基底负载扭矩（N·m）
    active_fault: str | None = None      # 注入哪类故障（None=正常）
    fault_component: str = ""               # 故障作用在哪个关节名
    fault_start_frac: float = 0.4           # 故障开始时间占比
    fault_end_frac: float = 0.9             # 故障结束时间占比

    def __post_init__(self):
        if not self.joints:
            prefixes = ("front_left_hip", "front_right_hip",
                        "front_left_knee", "front_right_knee")
            self.joints = [f"joint_motor.{prefixes[i % len(prefixes)]}"
                           for i in range(self.n_joints)]


@dataclass
class SynthSample:
    """一条合成的遥测样本。"""
    sample_id: str
    joints: list[str]
    dt: float
    # 时间序列（每关节一列）
    torque: np.ndarray      # shape (n_steps, n_joints)  N·m
    current: np.ndarray     # shape (n_steps, n_joints)  A
    temperature: np.ndarray # shape (n_steps, n_joints)  °C
    # 丢包掩码：1=观测正常，0=丢包（观测缺失）
    valid_mask: np.ndarray  # shape (n_steps, n_joints)
    # ground truth：One 段标签（部件, 起止时刻, 故障类型, 严重度）；正常样本为 None
    ground_truth: dict | None = None

    @property
    def n_steps(self) -> int:
        return int(self.torque.shape[0])

    def telemetry(self, joint: str, hour: int = 9, minute: int = 55,
                  sample_count: int = 300) -> dict:
        """把这份遥测转成 EvidenceStore 认识的窗口查询结果。

        取该关节时间序列的中间 sample_count 个点，做窗口统计，
        返回 { metrics, reference_window_metrics }。
        """
        idx = self.joints.index(joint)
        arr = self.temperature[:, idx]
        cur = self.current[:, idx]
        tor = self.torque[:, idx]
        v = self.valid_mask[:, idx]
        # 窗口：覆盖"整段样本"（每条样本=一个故障场景，时间窗即故障活跃期）。
        # 取整段，保证 Agent 能看到故障期间的抬升/丢包，而非只看末尾 3 秒。
        seg_t = arr
        seg_i = cur
        seg_tor = tor
        ref_t = arr[: len(arr) // 2] if len(arr) > 1 else arr  # 参照=前半段
        end_sec = int(round(minute * 60 + len(arr) * self.dt))
        end_m, end_s = divmod(end_sec, 60)
        # 参照电流：用前半段有效点的均值（排除故障期）
        ref_cur = cur[: len(cur) // 2]
        ref_cur = ref_cur[~np.isnan(ref_cur)] if np.isnan(ref_cur).any() else ref_cur
        loss = 1.0 - float(np.nanmean(v)) if len(v) else 0.0
        return {
            "window": f"{hour:02d}:{minute:02d}-{end_m:02d}:{end_s:02d}",
            "metrics": {
                "temperature_c": round(float(np.nanmean(seg_t)), 2),
                "current_a": round(float(np.nanmean(seg_i)), 2),
                "torque_nm": round(float(np.nanmean(seg_tor)), 2),
            },
            "reference_window_metrics": {
                "temperature_c": round(float(np.nanmean(ref_t)), 2),
                "current_a": round(float(np.nanmean(ref_cur)), 2),
            },
            "peak_temperature_c": round(float(np.nanmax(seg_t)), 2),
            "valid_mask": v.tolist(),
            "packet_loss_rate": round(loss, 3),
            "fault_ground_truth": self.ground_truth,
        }

    def to_dict(self) -> dict:
        return {
            "sample_id": self.sample_id,
            "joints": self.joints,
            "dt": self.dt,
            "n_steps": self.n_steps,
            "torque": self.torque.tolist(),
            "current": self.current.tolist(),
            "temperature": self.temperature.tolist(),
            "valid_mask": self.valid_mask.tolist(),
            "ground_truth": self.ground_truth,
        }


class Go2MotorModel:
    """一阶电机热 + 电机电流模型：给定负载扭矩/速度 → 电流 + 温度演化。"""

    def __init__(self, params: dict[str, float] | None = None):
        p = {**DEFAULT_PARAMS, **(params or {})}
        self.kt = p["kt"]
        self.R = p["R"]
        self.Cth = p["Cth"]
        self.Rth = p["Rth"]
        self.T_amb = p["T_amb"]

    def current_from_torque(self, torque: float) -> float:
        """稳态电流 = 等效扭矩 / 扭矩常数（含电阻压降的近似）。"""
        return abs(torque) / self.kt + 0.3  # +0.3 常数项近似铁损/励磁

    def step_temp(self, temp: float, current: float, dt: float) -> float:
        """一阶热模型：dT = (I²R - (T - Tamb)/Rth) / Cth * dt。"""
        heat = current * current * self.R
        cool = (temp - self.T_amb) / self.Rth
        return temp + (heat - cool) * dt / self.Cth


def _fault_time_window(t_total: float, start_frac: float, end_frac: float) -> tuple[int, int]:
    """把故障起止比例换算成样本时间窗字符串。"""
    s = int(t_total * start_frac)
    e = int(t_total * end_frac)
    return s, e


def _fmt_time(sec: int) -> str:
    m, s = divmod(sec, 60)
    return f"{m:02d}:{s:02d}"


def generate_sample(
    config: SynthGenConfig,
    params: dict[str, float] | None = None,
) -> SynthSample:
    """生成一条合成遥测样本，注入 config.active_fault 指定的故障。"""
    p = {**DEFAULT_PARAMS, **(params or {})}
    rng = np.random.default_rng(config.seed)
    n = config.n_joints
    n_steps = int(config.n_seconds / config.dt)
    dt = config.dt

    model = Go2MotorModel(p)
    stall_torque = p["stall_torque"]

    torque = np.zeros((n_steps, n))
    current = np.zeros((n_steps, n))
    temperature = np.full((n_steps, n), p["T_amb"])
    valid = np.ones((n_steps, n), dtype=float)

    # 基底负载扭矩：平缓波动（似正常行走/巡检）
    base = p["base_load_torque"] if "base_load_torque" in p else config.base_load_torque

    for j in range(n):
        temp = p["T_amb"]
        for t in range(1, n_steps):
            t_sec = t * dt
            # 基础负载 + 平滑随机波动
            noise = rng.normal(0, 0.3)
            T_load = base + 0.5 * np.sin(t_sec / 5.0) + noise

            # 故障注入（仅作用在 config.fault_component 对应关节）
            is_fault_joint = (config.fault_component == "" or
                              config.joints[j] == config.fault_component)
            in_fault = is_fault_joint and (
                config.fault_start_frac * config.n_seconds <= t_sec
                <= config.fault_end_frac * config.n_seconds
            )

            if in_fault and config.active_fault == "F1_stall":
                # 堵转：扭矩骤升到限幅
                T_load = stall_torque
            elif in_fault and config.active_fault == "F5_load_spike":
                # 外部大负载：一段突增
                T_load += stall_torque * 0.6
            elif in_fault and config.active_fault == "F2_temp_rise":
                # 热退化：散热失效 → 降低散热（相当于 Rth 变大），持续大负载
                T_load += stall_torque * 0.25
                model.Rth = p["Rth"] * (1 + 0.6)  # 散热变差

            # 电流：受故障影响的偏置
            cur = model.current_from_torque(T_load)
            if in_fault and config.active_fault == "F4_current_bias":
                cur += 5.0  # 电流传感器零漂（偏置明显到可测，覆盖后段拉高均值）
            # 堵转限流
            cur = min(cur, stall_torque / model.kt + 2.0)

            torque[t, j] = T_load
            current[t, j] = cur
            # 温度演化
            temp = model.step_temp(temp, cur, dt)
            temperature[t, j] = temp

    # F3 通信丢包：随机把一部分观测置为无效（NaN）
    if config.active_fault == "F3_packet_loss":
        for j in range(len(config.joints)):
            if config.fault_component and config.joints[j] != config.fault_component:
                continue
            drop = rng.random(n_steps) < 0.15
            start_t = config.fault_start_frac * config.n_seconds
            end_t = config.fault_end_frac * config.n_seconds
            drop &= np.arange(n_steps) * dt >= start_t
            drop &= np.arange(n_steps) * dt <= end_t
            valid[drop, j] = 0.0
            temperature[drop, j] = np.nan
            current[drop, j] = np.nan
            torque[drop, j] = np.nan

    # 观测高斯噪声（有效点上）
    from numpy.random import default_rng
    rr = default_rng(config.seed + 999)
    noise_t = rr.normal(0, config.noise_scale * 50, temperature.shape)  # 温度噪声 ~1°C
    noise_i = rr.normal(0, config.noise_scale * 3.0, current.shape)
    noise_tor = rr.normal(0, config.noise_scale * 20, torque.shape)
    temperature = np.where(valid == 1, temperature + noise_t, np.nan)
    current = np.where(valid == 1, current + noise_i, np.nan)
    torque = np.where(valid == 1, torque + noise_tor, np.nan)

    # ground truth
    gt = None
    if config.active_fault and config.fault_component:
        s_sec = int(config.fault_start_frac * config.n_seconds)
        e_sec = int(config.fault_end_frac * config.n_seconds)
        gt = {
            "sample_id": config.active_fault,
            "component": config.fault_component,
            "fault": config.active_fault,
            "severity": FAULT_DEFS[config.active_fault]["severity"],
            "window": f"{_fmt_time(s_sec)}~{_fmt_time(e_sec)}",
            "desc": FAULT_DEFS[config.active_fault]["desc"],
        }

    return SynthSample(
        sample_id=f"syn_{config.active_fault or 'normal'}_{config.seed}",
        joints=config.joints,
        dt=dt,
        torque=torque,
        current=current,
        temperature=temperature,
        valid_mask=valid,
        ground_truth=gt,
    )


def synthesize_batch(cfgs: list[SynthGenConfig],
                     params: dict[str, float] | None = None) -> list[SynthSample]:
    """批量生成多条样本（正常 + 各类故障，便于评估对比）。"""
    return [generate_sample(cfg, params) for cfg in cfgs]
