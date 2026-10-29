"""仿真遥测 → 真机 `LowState_` 帧 → DDS `rt/lowstate`。

这一层是**协议层**：它不再是"语义级 JSON"，而是 Unitree 厂商真正的
`unitree_go.msg.dds_.LowState_` 消息，走真机同款 DDS 通道。

为什么要分两个文件/两段
-----------------------
1. **纯映射**（`build_low_state_kwargs`）：`Telemetry` → `LowState_` 的字段字典。
   不 import cyclonedds，不碰 DDS，可以直接单测。字段对齐的坑全在这里。
2. **DDS 出口**（`DdsTelemetryPublisher`）：惰性 import SDK，发布到 `rt/lowstate`。
   依赖缺失时是**空实现**，与 `mqtt_pub.MqttTelemetryPublisher` 同一套约定。

**这个划分不是洁癖**：`cyclonedds` 是可选依赖（`data-dog[dds]`）。若把映射
逻辑和 SDK 绑在同一个 import 路径上，没装 DDS 的环境连映射单测都跑不了——
而映射恰恰是最需要钉住的部分（见下方关节顺序）。

关节槽位顺序：**仿真器与真机是反的**
------------------------------------
这是本文件最重要的一条，写错会让"左右对调 + 腿序错位"，而且**看起来完全正常**
（12 个槽都填了数、量纲也对，只是填错了腿）。

厂商出处（SDK 官方示例 `example/go2/low_level/unitree_legged_const.py`）::

    LegID = {"FR_0": 0, "FR_1": 1, "FR_2": 2,    # 前右 hip/thigh/calf
             "FL_0": 3, "FL_1": 4, "FL_2": 5,    # 前左
             "RR_0": 6, "RR_1": 7, "RR_2": 8,    # 后右
             "RL_0": 9, "RL_1": 10, "RL_2": 11}  # 后左

即真机顺序是 **FR → FL → RR → RL**。

而本仿真器的 `mujoco_sim.simulate.ACTUATORS` 是::

    ["FL_hip", "FL_thigh", "FL_calf",
     "FR_hip", "FR_thigh", "FR_calf",
     "RL_hip", "RL_thigh", "RL_calf",
     "RR_hip", "RR_thigh", "RR_calf"]

即 **FL → FR → RL → RR**。两边前两腿与后两腿**各自对调**。

所以绝不能按 `range(12)` 顺次填。`_SLOT_OF_JOINT` 是唯一权威，改这里必须
同时改它的对称单测（`test_dds_bridge.py` 里有一条逐槽断言的用例）。

`thigh` 填进 `LegID[*_1]` 不是笔误
----------------------------------
本项目历史遗留：检测器侧的部件名把承重主关节叫 `knee`，而物理上对应
MuJoCo 的 `thigh`（`_JOINT_TO_COMPONENT` 里 `FL_thigh → front_left_knee`）。
**IDL 这边要按物理名填**——`LegID["FL_1"]` 的注释就是 "Front left thigh"，
厂商的 `_1` 是 thigh 位。两套命名各守各的领域，映射在 `_SLOT_OF_JOINT` 一处完成。

未填的 8 个槽
-------------
`motor_state` 是 `array[MotorState_, 20]`，Go2 只用前 12 个。13–20 号槽
**必须置零并发出去**（DDS 是定长数组，不是变长列表），不能截断——
截断会改变序列化布局，收端直接解析错位。这也是"为什么不能只发 12 个"的原因。
"""
from __future__ import annotations

import math
import threading
from typing import Any

# 12 个电机在 `motor_state[20]` 里的槽位。**唯一权威**，见模块 docstring。
#
# key = 仿真器 `Telemetry.joints` 里的关节名（= `ACTUATORS` 的元素）
# value = `LowState_.motor_state` 的下标（= SDK `LegID` 的十进制值）
_SLOT_OF_JOINT: dict[str, int] = {
    # 前右 FR_0/1/2 = 0/1/2
    "FR_hip": 0, "FR_thigh": 1, "FR_calf": 2,
    # 前左 FL_0/1/2 = 3/4/5
    "FL_hip": 3, "FL_thigh": 4, "FL_calf": 5,
    # 后右 RR_0/1/2 = 6/7/8
    "RR_hip": 6, "RR_thigh": 7, "RR_calf": 8,
    # 后左 RL_0/1/2 = 9/10/11
    "RL_hip": 9, "RL_thigh": 10, "RL_calf": 11,
}

# `LowState_` 里 `motor_state` 的定长槽数。IDL: `array[MotorState_, 20]`。
MOTOR_SLOTS = 20

# Go2 实际使用的电机数（四足 × 3 关节）。
GO2_MOTORS = 12

# `BmsState_.cell_vol` 的定长槽数。IDL: `array[uint16, 15]`。
CELL_SLOTS = 15

# Go2 电池实际串数（6S）。见 `subsystems.BATTERY["cells"]`。
GO2_CELLS = 6

# `fan_frequency` 的定长槽数。IDL: `array[uint16, 4]`。
FAN_SLOTS = 4


def _clamp_int(value: float, lo: int, hi: int) -> int:
    """把浮点读数安全转成定宽整数，并夹在无符号/有符号量程内。

    为什么必须夹：`temperature` 是 `uint8`（0–255）、`lost` 是 `uint32`。
    仿真里温度已经被 `MOTOR_TEMP_CEILING=127` 钳过，但**丢包/故障注入仍可能
    产生 NaN 或负值**。`int(nan)` 在 Python 会抛 `ValueError`，在 C 扩展里
    可能直接写坏内存——两种都是把整个会话打断。这里统一给"量程内最近值"：
    NaN → `lo`（没有读数时给最保守的下界，而不是随机数）。
    """
    if not math.isfinite(value):
        return lo
    return max(lo, min(hi, int(round(value))))


def _slot_rows(t) -> dict[int, dict[str, Any]]:  # noqa: ANN001 - Telemetry，避免循环 import
    """把 `Telemetry` 的 12 路数组按**真机槽位**展开成 12 条电机读数。

    返回 `{slot: {q, dq, ...}}`，槽位是 `_SLOT_OF_JOINT` 算出来的。
    """
    rows: dict[int, dict[str, Any]] = {}
    joints = list(t.joints)
    for idx, joint in enumerate(joints):
        slot = _SLOT_OF_JOINT.get(joint)
        if slot is None:
            # 关节名不在表里说明 `ACTUATORS` 变了。**宁可炸**也不能静默跳过——
            # 静默的后果是某条腿永远发 0，而界面上一切正常。
            raise KeyError(
                f"关节 {joint!r} 不在真机槽位表里；"
                f"`ACTUATORS` 变了就必须同步 `_SLOT_OF_JOINT`（见模块 docstring）"
            )
        rows[slot] = {
            "mode": 0,
            "q": float(t.q[idx]),
            "dq": float(t.dq[idx]),
            "ddq": float(t.ddq[idx]),
            "tau_est": float(t.torque[idx]),
            "q_raw": float(t.q_raw[idx]),
            "dq_raw": float(t.dq_raw[idx]),
            "ddq_raw": float(t.ddq_raw[idx]),
            "temperature": _clamp_int(float(t.temperature[idx]), 0, 255),
            # `lost` 是**累计**计数（uint32），不是 0/1 标志位。
            # 详见 `rig.Telemetry.lost` 的注释：标志位会让 delta 恒 0。
            "lost": _clamp_int(float(t.lost[idx]), 0, 0xFFFFFFFF),
            # IDL `MotorState.reserve: array[uint32, 2]`，保留位。
            # 填任何值都是编造，一律置零（理由同顶层 `reserve`）。
            "reserve": [0, 0],
        }
    return rows


def build_low_state_kwargs(t) -> dict[str, Any] | None:  # noqa: ANN001
    """`Telemetry` → `LowState_` 的构造参数（纯数据，不含任何 DDS 对象）。

    返回 `None` 表示**这一刻不该发帧**（没有遥测）。调用方据此跳过。

    为什么返回 kwargs 而不是直接构造 `LowState_`：这样本函数零依赖、
    可单测；真正构造消息由 `DdsTelemetryPublisher._make_message` 做。

    **字段对应关系（逐条交代，不留"看起来差不多"）**

    MotorState（12 槽，按 `_SLOT_OF_JOINT`；未用的 8 槽置零）::

        mode        恒 0（仿真恒为 RPC 模式，IDL 里没有更真实的取值可给）
        q/dq/ddq    直接取，单位 rad / rad/s / rad/s²
        tau_est     关节扭矩 N·m
        q_raw/dq_raw/ddq_raw  未滤波读数（F6 编码器跳变只在 raw vs q 上暴露）
        temperature 电机温度，uint8。**项目仍为 °C 直填**（见下方"已知偏差"）
        lost        累计失联计数，uint32

    IMU（`imu_state`）::

        quaternion  w,x,y,z  —— 直接给四元数，不是 rpy 换算
        gyroscope   rad/s
        accelerometer m/s²
        rpy         rad
        temperature uint8

    BMS（`bms_state`）与顶层电源::

        power_v / power_a   LowState 顶层，float32，V / A
        soc                 uint8，%
        current             uint16 —— **单位未确认**，见"已知偏差"
        cycle               充电循环计数，uint16
        bq_ntc / mcu_ntc    uint8
        cell_vol            15 槽 uint16，**只填前 6 串，其余填 0**

    散热（顶层）::

        fan_frequency       uint16[4] —— **归一化浮点发不进去**，见"已知偏差"
        temperature_ntc1    uint8
        bandwidth           uint16，总线条数计数

    已知偏差（**必须知道，不要当成正确实现**）
    -------------------------------------------
    1. **`fan_frequency` 量纲对不上。** 仿真里是归一化 0..1 浮点
       （`ThermalState.fan_frequency`），IDL 是 `uint16[4]`。厂商公开材料
       **没给单位**（`vendor_docs/` 全查过，只有类型）。本实现按"占空比千分比"
       `round(x * 1000)` 映射，并**在载荷里标明这是假设**——不装作知道。
    2. **`BmsState.current` 单位未确认。** IDL 是 `uint16`，仿真里是浮点安培。
       厂商材料没写是 mA 还是 10mA 还是 A。本实现按 **mA**（`round(A*1000)`）
       映射——这是**假设**，理由是 6S/6.8Ah 的放电量级用 mA 才放得下且不丢精度。
       拿到真机帧后应立刻核对这一项。
    3. **`cell_vol` 单位未确认。** IDL 是 `uint16`，按 **mV** 映射（3.6V → 3600），
       同样是**假设**。`uint16` 放得下 0–65.5V 的 mV 值，是合理猜测但非事实。
    4. **`temperature` 沿用项目既有口径**（°C 直填 int）。厂商头文件
       `motor_msg_GO-M8010-6.h` 写的是 `int8_t temp; // -128~127°C`，
       即真机是 int8 且**可负**；IDL 却是 `uint8`。两处矛盾，本项目历史上一贯
       按 °C 无符号填（电机温度不会低于 0），这里保持一致，不在这层引入新口径。
    5. **未填的顶层字段**（`head` / `sn` / `version` / `foot_force*` /
       `wireless_remote` / `bit_flag` / `adc_reel` / `temperature_ntc2` /
       `reserve` / `crc`）一律置零。**`crc` 置零是诚实的**：真要算 CRC，
       得按 `unitree_sdk2py/utils/crc.py` 的算法逐字段累加，而本仓库没有
       真机可对账。置零 → 收端能立刻看出"这帧没算 CRC"，比填个假 CRC 好。
    """
    motors = _slot_rows(t)

    # 20 槽定长数组：12 个真实 + 8 个零槽。**顺序即槽位**，不能排序、不能压缩。
    motor_state: list[dict[str, Any]] = []
    for slot in range(MOTOR_SLOTS):
        motor_state.append(
            motors.get(slot) or {
                "mode": 0, "q": 0.0, "dq": 0.0, "ddq": 0.0, "tau_est": 0.0,
                "q_raw": 0.0, "dq_raw": 0.0, "ddq_raw": 0.0,
                "temperature": 0, "lost": 0, "reserve": [0, 0],
            }
        )

    imu = t.imu
    imu_kwargs: dict[str, Any] = {
        "quaternion": [1.0, 0.0, 0.0, 0.0],
        "gyroscope": [0.0, 0.0, 0.0],
        "accelerometer": [0.0, 0.0, -9.81],
        "rpy": [0.0, 0.0, 0.0],
        "temperature": 0,
    }
    if imu is not None:
        imu_kwargs = {
            "quaternion": [float(v) for v in imu.quat],
            "gyroscope": [float(v) for v in imu.gyro],
            "accelerometer": [float(v) for v in imu.accel],
            "rpy": [float(v) for v in imu.rpy],
            "temperature": _clamp_int(float(imu.temperature), 0, 255),
        }

    cell_vol = [0] * CELL_SLOTS
    bms_kwargs: dict[str, Any] = {
        "version_high": 0, "version_low": 0, "status": 0, "soc": 0,
        "current": 0, "cycle": 0, "bq_ntc": [0, 0], "mcu_ntc": [0, 0],
        "cell_vol": cell_vol,
    }
    power_v = 0.0
    power_a = 0.0
    if t.battery is not None:
        b = t.battery
        # 只填真机有的串数（6），其余保持 0。**不按 15 槽平均分摊**——
        # 那会把"6 串电池"伪装成"15 串且有两串是 0"，任何均衡性判据都会误报。
        ncells = min(len(b.cell_vol), GO2_CELLS, CELL_SLOTS)
        for i in range(ncells):
            cell_vol[i] = _clamp_int(float(b.cell_vol[i]) * 1000.0, 0, 0xFFFF)  # V → mV（假设）
        bms_kwargs = {
            "version_high": 0, "version_low": 0,
            "status": _clamp_int(float(b.status), 0, 255),
            "soc": _clamp_int(float(b.soc), 0, 100),
            # A → mA（**单位假设**，见 docstring 已知偏差 2）
            "current": _clamp_int(float(b.current) * 1000.0, 0, 0xFFFF),
            "cycle": _clamp_int(float(b.cycle), 0, 0xFFFF),
            "bq_ntc": [_clamp_int(float(b.bq_ntc), 0, 255)] * 2,
            "mcu_ntc": [_clamp_int(float(b.mcu_ntc), 0, 255)] * 2,
            "cell_vol": cell_vol,
        }
        power_v = float(b.voltage)
        power_a = float(b.current)

    fan = [0] * FAN_SLOTS
    ntc1 = 0
    bandwidth = 0
    if t.thermal is not None:
        th = t.thermal
        # 归一化 0..1 → 千分比（**单位假设**，见 docstring 已知偏差 1）
        fan = [_clamp_int(float(th.fan_frequency) * 1000.0, 0, 0xFFFF)] * FAN_SLOTS
        ntc1 = _clamp_int(float(th.temperature_ntc1), 0, 255)
        bandwidth = _clamp_int(float(th.bandwidth), 0, 0xFFFF)

    return {
        "head": [0, 0],
        "level_flag": 0,
        "frame_reserve": 0,
        "sn": [0, 0],
        "version": [0, 0],
        "bandwidth": bandwidth,
        "imu_state": imu_kwargs,
        "motor_state": motor_state,
        "bms_state": bms_kwargs,
        "foot_force": [0, 0, 0, 0],
        "foot_force_est": [0, 0, 0, 0],
        "tick": 0,                       # 由调用方按仿真步填
        "wireless_remote": [0] * 40,
        "bit_flag": 0,
        "adc_reel": 0.0,
        "temperature_ntc1": ntc1,
        "temperature_ntc2": 0,
        "power_v": power_v,
        "power_a": power_a,
        "fan_frequency": fan,
        "reserve": 0,
        "crc": 0,                        # 置零而非编造，理由见 docstring 已知偏差 5
    }


class DdsStats:
    """DDS 出口的计数。与 `mqtt_pub.MqttStats` 同构，便于页面统一展示。"""

    __slots__ = ("published", "failed", "connected", "last_error")

    def __init__(self) -> None:
        self.published = 0
        self.failed = 0
        self.connected = False
        self.last_error: str | None = None


class DdsTelemetryPublisher:
    """把遥测组装成 `LowState_` 并发到 DDS `rt/lowstate`。

    可选依赖（`data-dog[dds]`）。**依赖缺失时是空实现**——调用方无需分支，
    `state()` 里 `enabled=False` 并给出原因，与 MQTT 出口的约定一致。

    为什么不抛异常：这是演示路径上的第二条出口，DDS 挂了不该让整个仿真停。
    但**不许静默**——`failed` 计数 + `last_error` 首次原因都必须留给页面看。

    线程安全
    --------
    DDS 的 `DataWriter.write()` 是线程安全的（CycloneDDS 保证），但本类仍然
    **加锁**：`close()` 与 `publish()` 并发时，写已释放的 writer 会崩在 C 层，
    Python 的 try/except 拦不住进程级访问违例。这把锁防的是这个。
    """

    def __init__(self, *, topic: str = "rt/lowstate", domain: int = 0,
                 interface: str | None = None, enabled: bool = True) -> None:
        self.topic = topic
        self.domain = int(domain)
        self.interface = interface
        self.stats = DdsStats()
        self._enabled = bool(enabled)
        self._lock = threading.Lock()
        self._pub: Any = None
        self._msg_cls: Any = None
        self._motor_cls: Any = None
        self._imu_cls: Any = None
        self._bms_cls: Any = None

        if not self._enabled:
            self.stats.last_error = "已关闭"
            return

        try:
            from unitree_sdk2py.core.channel import (  # noqa: F401
                ChannelFactoryInitialize,
                ChannelPublisher,
            )
            from unitree_sdk2py.idl.unitree_go.msg.dds_ import (
                BmsState_,
                IMUState_,
                LowState_,
                MotorState_,
            )
        except ModuleNotFoundError as e:
            self.stats.last_error = (
                f"缺少 {e.name}：pip install 'data-dog[dds]' "
                f"并装 unitree_sdk2py（见 vendor_docs/unitree/SOURCES.md）"
            )
            return

        try:
            # `ChannelFactoryInitialize` 只能成功一次（进程级单例）。重复调用
            # 在 SDK 里是幂等的，但若 domain 不同会静默沿用第一次的——所以
            # 这里只做一次，且把结果记进 last_error 供排查。
            ChannelFactoryInitialize(self.domain, self.interface)
        except Exception as e:              # noqa: BLE001
            self.stats.last_error = f"ChannelFactoryInitialize 失败：{type(e).__name__}: {e}"
            return

        try:
            self._msg_cls = LowState_
            self._motor_cls = MotorState_
            self._imu_cls = IMUState_
            self._bms_cls = BmsState_
            self._pub = ChannelPublisher(self.topic, LowState_)
            self._pub.Init()
        except Exception as e:              # noqa: BLE001
            self.stats.last_error = f"发布者初始化失败：{type(e).__name__}: {e}"
            self._pub = None
            return

        self.stats.connected = True

    # ---------------------------------------------------------------- 构造
    def _make_message(self, kwargs: dict[str, Any]) -> Any:
        """把纯映射的 kwargs 变成真正的 `LowState_` 实例。"""
        motor = [self._motor_cls(**m) for m in kwargs["motor_state"]]
        imu = self._imu_cls(**kwargs["imu_state"])
        bms = self._bms_cls(**kwargs["bms_state"])
        rest = {k: v for k, v in kwargs.items()
                if k not in ("motor_state", "imu_state", "bms_state")}
        return self._msg_cls(motor_state=motor, imu_state=imu, bms_state=bms, **rest)

    # ---------------------------------------------------------------- 发布
    def publish_telemetry(self, telemetry, *, tick: int = 0) -> bool:  # noqa: ANN001
        """一条 `Telemetry` → 一帧 `LowState_`。返回是否真的写进了 DDS。"""
        if self._pub is None:
            return False
        try:
            kwargs = build_low_state_kwargs(telemetry)
            if kwargs is None:
                return False
            kwargs["tick"] = _clamp_int(float(tick), 0, 0xFFFFFFFF)
            msg = self._make_message(kwargs)
            with self._lock:
                if self._pub is None:
                    return False
                self._pub.Write(msg)
            with self._lock:
                self.stats.published += 1
            return True
        except Exception as e:              # noqa: BLE001
            with self._lock:
                self.stats.failed += 1
                if self.stats.failed == 1 or self.stats.failed % 100 == 0:
                    self.stats.last_error = (
                        f"{type(e).__name__}: {e} (共 {self.stats.failed} 帧)")
            return False

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": self._enabled, "topic": self.topic,
                    "domain": self.domain, "interface": self.interface,
                    "published": self.stats.published,
                    "failed": self.stats.failed,
                    "connected": self.stats.connected,
                    "last_error": self.stats.last_error}

    def close(self) -> None:
        """释放 writer。**不能和 `publish_telemetry` 并发**——锁住。"""
        with self._lock:
            self._pub = None
            self.stats.connected = False


__all__ = [
    "CELL_SLOTS", "DdsTelemetryPublisher", "GO2_CELLS", "GO2_MOTORS",
    "MOTOR_SLOTS", "build_low_state_kwargs",
]
