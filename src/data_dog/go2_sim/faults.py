"""故障库：可随机抽取的"病因"，每条自带注入层级、作用子系统与物理故事。

覆盖边界的诚实声明
------------------
"机器狗所有可能出现的故障"在工程上必须落成一个**可检测**的集合，而不是无止境
的清单。本模块的边界是：**凡是能在厂商 IDL 给出的可观测通道上留下痕迹的故障，
都纳入；凡是遥测看不见的（外壳破裂、线缆松脱但尚未影响信号），不纳入。**

可观测通道全部来自 `vendor_docs/unitree/cleaned/raw/` 下的 IDL：
  - MotorState : mode / q / dq / tau_est / q_raw / dq_raw / temperature / lost
  - IMUState   : quaternion / gyroscope / accelerometer / rpy / temperature
  - BmsState   : status / soc / current / cycle / bq_ntc / mcu_ntc / cell_vol
  - LowState   : power_v / power_a / temperature_ntc1/2 / fan_frequency / bandwidth
  - SportModeState : error_code / mode / gait_type / foot_force / body_height
  - Error      : source / state

为什么要分注入层
----------------
同样的现象，三层的处置完全不同（物理层要停机检修，观测层换个传感器就行，
热层清理散热即可）。Agent 报告里必须能区分，否则"诊断"就是一句空话。

  - **物理层**：改仿真本身（施加力/扭矩、改环境）→ 扭矩、电流、姿态全都动。
  - **热层**：电机没坏，但散热坏了（积灰、风扇停转）→ 温度爬升而电流正常。
  - **观测层**：机器好好的，坏的是传感器 / 通信 → 数据看起来不对。
  - **电气层**：电源侧出问题 → 影响的不是一块电机，而是**母线下游全体**。
    这是区分"电池故障"与"单电机故障"的根因判据。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Layer(str, Enum):
    """故障注入作用的层。"""

    PHYSICS = "physics"          # 改仿真本身：施加外部力/扭矩、环境
    THERMAL = "thermal"          # 改热模型：散热热阻、产热、环境温度
    OBSERVATION = "observation"  # 只改"读到的数"：偏置、丢包、CRC
    ELECTRICAL = "electrical"    # 电源侧：母线电压、电池单体、过流


class Subsystem(str, Enum):
    """故障所属子系统（与厂商 IDL 的结构对齐）。"""

    JOINT = "joint"          # 关节/电机
    THERMAL = "thermal"      # 热管理（风扇、散热）
    POWER = "power"          # 电源/电池
    IMU = "imu"              # 惯性测量
    COMMS = "comms"          # 通信总线
    SYSTEM = "system"        # 整机/步态


@dataclass(frozen=True)
class FaultSpec:
    """一条可注入的故障。"""

    key: str                     # 与数据集 JSONL 的 `fault` 字段同名（旧 5 类保持兼容）
    label: str
    layer: Layer
    subsystem: Subsystem
    severity: str                # info / warning / critical
    story: str                   # 物理故事：现场发生了什么
    symptom: str                 # 数据上应该看到什么
    component_hint: str = "joint_motor.front_left_hip"  # 默认作用部件
    # 该故障在哪些可观测字段上留痕（供 UI 提示与测试断言用）
    observable: tuple[str, ...] = ()

    def describe(self) -> dict[str, object]:
        return {
            "key": self.key, "label": self.label, "layer": self.layer.value,
            "subsystem": self.subsystem.value, "severity": self.severity,
            "story": self.story, "symptom": self.symptom,
            "component_hint": self.component_hint,
            "observable": list(self.observable),
        }


# 每条故障的注入难度分级：sim=可在当前 MuJoCo 层真实注入；
# field=只能注入读数（观测层）；model=需要额外子模型（BMS/IMU/风扇）。
# 这个标注是给用户看"哪些已经是真物理、哪些还是读数级"的，不在运行时使用。
FAULT_LIBRARY: tuple[FaultSpec, ...] = (
    # ---------------------------------------------------------------- 关节/电机
    FaultSpec(
        key="normal", label="正常", layer=Layer.PHYSICS,
        subsystem=Subsystem.JOINT, severity="info",
        story="机器狗状态正常，无故障注入。",
        symptom="遥测平稳，温度停留在一阶热平衡点。",
        observable=(),
    ),
    FaultSpec(
        key="F1_stall", label="堵转", layer=Layer.PHYSICS,
        subsystem=Subsystem.JOINT, severity="critical",
        story="腿被台阶/岩石卡住，电机满扭矩硬顶但关节转不动。",
        symptom="扭矩饱和、电流冲到限幅、温度在几十秒内越过 90°C 保护线。",
        observable=("tau_est", "current", "temperature", "dq"),
    ),
    FaultSpec(
        key="F6_encoder_jump", label="编码器跳变", layer=Layer.OBSERVATION,
        subsystem=Subsystem.JOINT, severity="warning",
        story="编码器受电磁干扰或码盘磨损，q 读数出现阶跃，与 q_raw 对不上。",
        symptom="q 相对 q_raw 出现固定偏移或跳变，d q 出现尖峰；电流不受影响。",
        observable=("q", "q_raw", "dq"),
    ),
    FaultSpec(
        key="F7_tau_est_fail", label="力矩估算失效", layer=Layer.OBSERVATION,
        subsystem=Subsystem.JOINT, severity="warning",
        story="FOC 电流环的力矩估算系数漂了，tau_est 与真实 |I|·kt 不再一致。",
        symptom="tau_est 系统性偏离 |current|*kt 的比例，温度电流关系被打乱。",
        observable=("tau_est", "current"),
    ),
    FaultSpec(
        key="F8_motor_lost", label="电机失联", layer=Layer.OBSERVATION,
        subsystem=Subsystem.JOINT, severity="critical",
        story="电机驱动器通信中断，该关节的 lost 计数持续上涨。",
        symptom="该关节 lost 递增、mode 位异常，其余关节正常。",
        observable=("lost", "mode"),
    ),
    FaultSpec(
        key="F4_current_bias", label="电流传感器零漂", layer=Layer.OBSERVATION,
        subsystem=Subsystem.JOINT, severity="warning",
        story="电流传感器的零点漂了，读数整体偏高，但电机本身工作正常。",
        symptom="电流读数整体平移一个固定量，温度却因真实电流正常而不跟着涨。",
        observable=("current", "temperature"),
    ),
    # ------------------------------------------------------------------ 热管理
    FaultSpec(
        key="F2_temp_rise", label="散热失效", layer=Layer.THERMAL,
        subsystem=Subsystem.THERMAL, severity="warning",
        story="电机没坏，但散热片积灰/风扇停转，热量散不出去。",
        symptom="电流正常，温度却持续爬升——负载没变而温度变了，这是关键特征。",
        observable=("temperature", "current"),
    ),
    FaultSpec(
        key="F9_fan_stalled", label="风扇停转", layer=Layer.THERMAL,
        subsystem=Subsystem.THERMAL, severity="warning",
        story="散热风扇卡死或供电丢失，fan_frequency 掉到 0。",
        symptom="fan_frequency 为 0，随后温度单调爬升；电流保持正常。",
        observable=("fan_frequency", "temperature"),
    ),
    FaultSpec(
        key="F10_ambient_hot", label="环境高温", layer=Layer.THERMAL,
        subsystem=Subsystem.THERMAL, severity="info",
        story="用户在高温环境运行（沙漠/正午），环境温度本身抬升。",
        symptom="**全体**关节温度同步上移一个台阶，彼此温差保持不变。",
        observable=("temperature",),
    ),
    # ---------------------------------------------------------------- 电源/电池
    FaultSpec(
        key="F11_battery_low", label="电量低", layer=Layer.ELECTRICAL,
        subsystem=Subsystem.POWER, severity="warning",
        story="电量接近耗尽，厂商固件提示 10 分钟内自动趴下。",
        symptom="soc 单调下降并越过告警线；母线电压随负载轻微下垂。",
        observable=("soc", "power_v"),
    ),
    FaultSpec(
        key="F12_cell_imbalance", label="单体电压不均衡", layer=Layer.ELECTRICAL,
        subsystem=Subsystem.POWER, severity="warning",
        story="某节电芯衰减快，充放电时单体电压被拉偏。",
        symptom="cell_vol 的极差（max-min）随负载增大而扩大。",
        observable=("cell_vol",),
    ),
    FaultSpec(
        key="F13_battery_hot", label="电池过温", layer=Layer.ELECTRICAL,
        subsystem=Subsystem.POWER, severity="critical",
        story="持续大电流放电或散热不良，电池包 NTC 温度越过安全线。",
        symptom="bq_ntc / mcu_ntc 抬升，与电机温度不同步。",
        observable=("bq_ntc", "mcu_ntc"),
    ),
    FaultSpec(
        key="F14_bus_voltage_drop", label="母线电压跌落", layer=Layer.ELECTRICAL,
        subsystem=Subsystem.POWER, severity="critical",
        story="电池内阻增大或主回路接触不良，母线电压被拉低。",
        symptom="**全体**电机电流同时抬升（恒功率补偿），功率 power_v 明显下降。",
        observable=("power_v", "current", "power_a"),
    ),
    FaultSpec(
        key="F15_battery_overcurrent", label="电池过流", layer=Layer.ELECTRICAL,
        subsystem=Subsystem.POWER, severity="critical",
        story="负载需求超过电池放电能力，BMS 电流越过限值。",
        symptom="BmsState.current 越过阈值并触发保护。",
        observable=("current",),
    ),
    # ------------------------------------------------------------------- IMU
    FaultSpec(
        key="F16_gyro_bias", label="陀螺零偏", layer=Layer.OBSERVATION,
        subsystem=Subsystem.IMU, severity="warning",
        story="IMU 陀螺轴零偏未校准，静止时也读数不为零。",
        symptom="机静止时 gyroscope 非零且稳定；姿态 rpy 缓慢漂移。",
        observable=("gyroscope", "rpy"),
    ),
    FaultSpec(
        key="F17_accel_fail", label="加速度计失效", layer=Layer.OBSERVATION,
        subsystem=Subsystem.IMU, severity="critical",
        story="加速度计校准错误或损坏，测到的重力方向不对。",
        symptom="静止时 accelerometer 模长偏离 1g，或方向与 quaternion 不符。",
        observable=("accelerometer", "quaternion"),
    ),
    FaultSpec(
        key="F18_fallen", label="翻倒", layer=Layer.PHYSICS,
        subsystem=Subsystem.IMU, severity="critical",
        story="踩空/被撞，机身倾覆触地，四腿悬空。",
        symptom="rpy 倾角超限、body_height 掉到触地高度、足端力消失。",
        observable=("rpy", "body_height", "foot_force"),
    ),
    # ------------------------------------------------------------------ 通信
    FaultSpec(
        key="F3_packet_loss", label="通信丢包", layer=Layer.OBSERVATION,
        subsystem=Subsystem.COMMS, severity="warning",
        story="WiFi 或总线抖动，一段时间的观测数据直接丢了。",
        symptom="观测出现随机空洞，有效样本率下降；真实物理状态完全正常。",
        observable=("lost", "valid"),
    ),
    FaultSpec(
        key="F19_crc_error", label="CRC 错误", layer=Layer.OBSERVATION,
        subsystem=Subsystem.COMMS, severity="warning",
        story="线束受干扰，帧校验频繁失败，坏帧被丢弃。",
        symptom="CRC 失败率上升，有效帧率下降；序列号出现缺口。",
        observable=("crc", "lost"),
    ),
    FaultSpec(
        key="F20_bandwidth_low", label="带宽下降", layer=Layer.OBSERVATION,
        subsystem=Subsystem.COMMS, severity="warning",
        story="总线负载过高或链路质量差，采样带宽下降。",
        symptom="bandwidth 字段低于正常值，采样周期变长。",
        observable=("bandwidth",),
    ),
    # ---------------------------------------------------------------- 整机/步态
    FaultSpec(
        key="F5_load_spike", label="负载骤增", layer=Layer.PHYSICS,
        subsystem=Subsystem.SYSTEM, severity="warning",
        story="有人往狗背上加载或它开始爬坡，需要持续更大的力才能维持动作。",
        symptom="扭矩与电流整体抬升，温度随之缓慢上升。",
        observable=("tau_est", "current", "temperature"),
    ),
    FaultSpec(
        key="F21_gait_anomaly", label="步态异常", layer=Layer.OBSERVATION,
        subsystem=Subsystem.SYSTEM, severity="warning",
        story="控制器下发的 gait_type 与实际运动不匹配（如报 trot 实际单腿拖行）。",
        symptom="gait_type 声明与 foot_force 的相位关系矛盾。",
        observable=("gait_type", "foot_force"),
    ),
    FaultSpec(
        key="F22_foot_slip", label="足端打滑", layer=Layer.PHYSICS,
        subsystem=Subsystem.SYSTEM, severity="warning",
        story="一只脚的脚垫磨秃/沾油，这条腿蹬地时抓不住地面，其余三条正常。",
        # 判据**不是**足端力：打滑的脚照样承重，力几乎不变
        # （实测跑步 27.2→26.8N）。也不是整片地面摩擦——四条腿一起滑
        # 只会让机身整体漂移、轨迹仍然笔直，读数上看不出来。
        # 单腿失抓的真实表现是**偏航**：实测 40s 内 Δy 由 −1.18m 翻成 +1.07m，
        # |yaw_speed| 由 0.173 升到 0.248 rad/s。这是"直线走成弧线"的现场症状。
        symptom="机身航向持续偏转、轨迹由直变弧；单腿 foot_force 偏低但仍承重。",
        observable=("yaw_speed", "position"),
    ),
    FaultSpec(
        key="F23_system_error", label="整机报错", layer=Layer.OBSERVATION,
        subsystem=Subsystem.SYSTEM, severity="critical",
        story="固件自检报错，error_code 非零（Error.source 指明来源模块）。",
        symptom="error_code 非零，source/state 指出出错模块。",
        observable=("error_code",),
    ),
)

_BY_KEY = {f.key: f for f in FAULT_LIBRARY}

# 按姿态调整的可疑故障：有些故障在某个姿态下**物理上不成立**，不该被推荐。
# 这里只做**默认推荐**，用户仍可手动选，不硬拦——但随机注入必须守住这条线，
# 否则演示里会出现"站着不动却打滑/步态异常"这种说不通的故事。
#
#   - 站立：腿不迈步 → 打滑(F22)、步态异常(F21)缺乏物理基础；
#           站得稳 → 翻倒(F18)也不是这个姿态该发生的。
#   - 奔跑：全部成立（这是运动态，打滑/步态异常/翻倒都真实）。
#   - 趴卧：腿几乎不受力 → 堵转(F1)、打滑(F22)、步态异常(F21)、
#           翻倒(F18)不成立；承重小 → 负载骤增(F5)不成立。
_MOTION_ONLY = frozenset({"F21_gait_anomaly", "F22_foot_slip"})
_RECOMMENDED = {
    "stand": tuple(k for k in _BY_KEY
                   if k not in _MOTION_ONLY and k != "F18_fallen"),
    "run": tuple(_BY_KEY),
    "prone": ("normal", "F2_temp_rise", "F3_packet_loss",
              "F4_current_bias", "F6_encoder_jump", "F7_tau_est_fail",
              "F8_motor_lost", "F9_fan_stalled", "F10_ambient_hot",
              "F11_battery_low", "F12_cell_imbalance", "F13_battery_hot",
              "F14_bus_voltage_drop", "F15_battery_overcurrent",
              "F16_gyro_bias", "F17_accel_fail", "F19_crc_error",
              "F20_bandwidth_low", "F23_system_error"),
}

# 旧 5 类（与 data/mj_* 数据集 `fault` 字段同名）——保留显式集合，
# 便于"数据集语义"与"全量故障"两条线各自引用。
LEGACY_KEYS = ("normal", "F1_stall", "F2_temp_rise", "F3_packet_loss",
               "F4_current_bias", "F5_load_spike")


def fault_by_key(key: str) -> FaultSpec:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise ValueError(
            f"未知故障 {key!r}；可用：{', '.join(_BY_KEY)}") from None


def recommended(posture: str) -> tuple[str, ...]:
    """该姿态下物理上说得通的故障集合。"""
    return _RECOMMENDED.get(posture, tuple(_BY_KEY))


def by_subsystem() -> dict[str, list[FaultSpec]]:
    """按子系统分组的故障清单（给 UI 分组展示用）。"""
    out: dict[str, list[FaultSpec]] = {}
    for f in FAULT_LIBRARY:
        out.setdefault(f.subsystem.value, []).append(f)
    return out


def random_pick(posture: str = "stand", *, rng=None,
                include_normal: bool = True) -> FaultSpec:
    """从该姿态可用的故障里随机抽一条（演示用的"随机注入"入口）。"""
    import random as _random

    keys = recommended(posture)
    if not include_normal:
        keys = tuple(k for k in keys if k != "normal")
    picker = rng or _random
    return _BY_KEY[picker.choice(list(keys))]


__all__ = ["FAULT_LIBRARY", "FaultSpec", "Layer", "Subsystem",
           "LEGACY_KEYS", "fault_by_key", "recommended", "by_subsystem",
           "random_pick"]
