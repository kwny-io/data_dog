"""DDS 桥接的**纯映射**测试：不装 cyclonedds 也能跑。

为什么把测试和 DDS 分开
-----------------------
`data-dog[dds]` 是可选依赖。若这里 `import cyclonedds`，CI（装的是
`.[dev,postgres]`）会整文件报错——而**最容易错、最需要钉住的恰恰是纯映射**
（关节槽位顺序、定长数组槽数、量纲换算），不是 DDS 本身。

真机帧的环回验证在 `scripts/dds_loopback.py` 里，那里才需要 DDS。

钉住的四件事
------------
1. **关节槽位顺序**（最重要）：仿真器是 FL→FR→RL→RR，真机是 FR→FL→RR→RL。
   写错的表现是"左右对调、看起来完全正常"。
2. **定长数组槽数**：`motor_state` 20、`cell_vol` 15、`fan_frequency` 4。
   DDS 是定长数组，截断会改变序列化布局，收端直接错位。
3. **未用槽必须置零且数量正确**：`cell_vol` 只填 6 串，其余 9 个是 0。
   **不能按 15 槽平摊**，那会把 6 串伪装成"15 串且有两串为 0"。
4. **NaN / 越界不许炸**：`int(nan)` 会 ValueError，把整个会话打断。

约定同 `tests/test_go2_sim_pure.py`：每个 test 结尾 `print("PASS <name>")`。
"""
from __future__ import annotations

import math

import numpy as np

from data_dog.go2_sim.dds_bridge import (
    _SLOT_OF_JOINT,
    CELL_SLOTS,
    GO2_CELLS,
    GO2_MOTORS,
    MOTOR_SLOTS,
    _clamp_int,
    build_low_state_kwargs,
)
from data_dog.go2_sim.subsystems import BatteryState, ImuState, ThermalState


class FakeTelemetry:
    """最小可用的遥测替身：只需 `build_low_state_kwargs` 读到的那些属性。"""

    def __init__(self, *, joints: list[str], n: int = 12,
                 temperature: float = 42.0) -> None:
        self.t = 1.0
        self.joints = list(joints)
        self.q = np.full(n, 0.11)
        self.dq = np.full(n, 1.2)
        self.ddq = np.full(n, 0.35)
        self.torque = np.full(n, 3.442)
        self.q_raw = np.full(n, 0.11)
        self.dq_raw = np.full(n, 1.2)
        self.ddq_raw = np.full(n, 0.34)
        self.temperature = np.full(n, temperature)
        self.lost = np.zeros(n, dtype=np.int64)
        self.imu = ImuState()
        self.battery = BatteryState()
        self.thermal = ThermalState()
        self.system = None


def _joints_from_actuators() -> list[str]:
    from data_dog.mujoco_sim.simulate import ACTUATORS
    return list(ACTUATORS)


def test_slot_order_matches_vendor_legid():
    """**核心用例**：槽位顺序必须与厂商 `LegID` 逐项一致。

    出处：SDK `example/go2/low_level/unitree_legged_const.py`

        FR_0=0 FR_1=1 FR_2=2  FL_0=3 FL_1=4 FL_2=5
        RR_0=6 RR_1=7 RR_2=8  RL_0=9 RL_1=10 RL_2=11

    这条用例故意**逐槽写死数字**，不按 `_SLOT_OF_JOINT` 反查——
    反查的话，把表整体改成错的顺序，测试也会跟着一起错，等于没测。
    """
    expected = {
        "FR_hip": 0, "FR_thigh": 1, "FR_calf": 2,
        "FL_hip": 3, "FL_thigh": 4, "FL_calf": 5,
        "RR_hip": 6, "RR_thigh": 7, "RR_calf": 8,
        "RL_hip": 9, "RL_thigh": 10, "RL_calf": 11,
    }
    assert _SLOT_OF_JOINT == expected, (
        "关节槽位顺序与厂商 LegID 不符——这会让真机上左右腿互换")
    print("PASS test_slot_order_matches_vendor_legid")


def test_slot_table_covers_every_actuator():
    """`ACTUATORS` 里的每个关节都必须在槽位表里，一个不能漏。

    漏了会在 `_slot_rows` 抛 KeyError（宁可炸也不静默），但这里提前钉住，
    让"改 ACTUATORS 忘了改槽位表"在 CI 就红，而不是等运行时。
    """
    actuators = set(_joints_from_actuators())
    assert actuators == set(_SLOT_OF_JOINT), (
        f"ACTUATORS 与槽位表不一致：仅 ACTUATORS 有 {actuators - set(_SLOT_OF_JOINT)}，"
        f"仅槽位表有 {set(_SLOT_OF_JOINT) - actuators}")
    assert len(_SLOT_OF_JOINT) == GO2_MOTORS == 12
    # 槽位必须是 0..11 的一个排列（不重不漏）
    assert sorted(_SLOT_OF_JOINT.values()) == list(range(12))
    print("PASS test_slot_table_covers_every_actuator")


def test_fr_hip_lands_in_slot_zero_not_fl_hip():
    """**反例钉死**：FL_hip 绝不能进 slot 0。

    这是"按 range(12) 顺序填"会犯的错：仿真器第 0 个是 FL_hip，
    真机第 0 个是 FR_hip。两者 q 值在对称站姿下几乎相同（差个符号），
    肉眼看不出，所以必须靠用例挡。
    """
    t = FakeTelemetry(joints=_joints_from_actuators())
    # 让 FL_hip 和 FR_hip 取**明显不同**的值，若错位立刻可辨
    t.q[0] = 0.50    # FL_hip（仿真第 0 个）
    t.q[3] = -0.70   # FR_hip（仿真第 3 个）
    kw = build_low_state_kwargs(t)
    assert kw["motor_state"][0]["q"] == -0.70, (
        "slot 0 必须是真机 FR_hip；把仿真第 0 个（FL_hip）填进去就是左右对调")
    assert kw["motor_state"][3]["q"] == 0.50
    print("PASS test_fr_hip_lands_in_slot_zero_not_fl_hip")


def test_motor_array_is_fixed_20_slots():
    """`motor_state` 必须是定长 20 槽，不能用 12 槽截断。

    IDL: `array[MotorState_, 20]`。DDS 定长数组少发会改变序列化布局，
    收端解析直接错位——不是"少几个字段"那么轻。
    """
    t = FakeTelemetry(joints=_joints_from_actuators())
    kw = build_low_state_kwargs(t)
    ms = kw["motor_state"]
    assert len(ms) == MOTOR_SLOTS == 20
    # 前 12 槽有真实数据，后 8 槽必须是零
    for i in range(12):
        assert ms[i]["q"] == 0.11, f"槽 {i} 应有真实数据"
        assert ms[i]["reserve"] == [0, 0], "reserve 是保留位，必须置零"
    for i in range(12, 20):
        assert ms[i]["q"] == 0.0 and ms[i]["temperature"] == 0, f"槽 {i} 应为零槽"
    print("PASS test_motor_array_is_fixed_20_slots")


def test_cell_vol_fills_six_and_leaves_nine_zero():
    """`cell_vol` 15 槽，只填前 6 串，其余 9 个必须为 0。

    **不能按 15 槽平摊**：Go2 电池是 6 串，把 6 串摊到 15 槽会让
    单体不均衡规则读到 9 个 0V 的"坏单体"，直接误报。
    """
    t = FakeTelemetry(joints=_joints_from_actuators())
    t.battery = BatteryState(cell_vol=[4.179, 4.175, 4.176, 4.172, 4.175, 4.180])
    kw = build_low_state_kwargs(t)
    cv = kw["bms_state"]["cell_vol"]
    assert len(cv) == CELL_SLOTS == 15
    # V → mV（单位假设，见模块 docstring）
    assert cv[:6] == [4179, 4175, 4176, 4172, 4175, 4180]
    assert cv[6:] == [0] * (CELL_SLOTS - GO2_CELLS) == [0] * 9
    print("PASS test_cell_vol_fills_six_and_leaves_nine_zero")


def test_battery_with_wrong_cell_count_is_truncated_not_padded():
    """电池串数超过 6 时截断到 6，不是往 15 槽里塞。

    仿真器若将来给 8 串，这里必须只取前 6——多出来的塞进去会让
    `cell_vol` 的语义（真机 6 串）失真。
    """
    t = FakeTelemetry(joints=_joints_from_actuators())
    t.battery = BatteryState(cell_vol=[4.0] * 8)
    kw = build_low_state_kwargs(t)
    cv = kw["bms_state"]["cell_vol"]
    assert cv[:6] == [4000] * 6
    assert cv[6:] == [0] * 9
    print("PASS test_battery_with_wrong_cell_count_is_truncated_not_padded")


def test_fan_frequency_is_four_slots():
    """`fan_frequency` 是 `uint16[4]`，归一化浮点发不进去，必须换算。

    仿真里是 0..1 浮点，IDL 是 uint16。本实现按千分比映射（**假设**，
    厂商材料没给单位），这里只钉住"4 槽、整数、可复现"。
    """
    t = FakeTelemetry(joints=_joints_from_actuators())
    t.thermal = ThermalState(fan_frequency=0.3)
    kw = build_low_state_kwargs(t)
    fan = kw["fan_frequency"]
    assert len(fan) == 4
    assert fan == [300, 300, 300, 300]
    assert all(isinstance(v, int) for v in fan), "uint16 不能是浮点"
    print("PASS test_fan_frequency_is_four_slots")


def test_all_integer_fields_are_int_not_float():
    """所有定宽整型字段必须是 Python int，不能漏成 float。

    DDS 序列化按 IDL 类型走，float 塞进 uint8 会在 C 层被截断或报错。
    这条按字段逐个检查，比"看起来对"可靠。
    """
    t = FakeTelemetry(joints=_joints_from_actuators(), temperature=42.6)
    kw = build_low_state_kwargs(t)
    for slot in kw["motor_state"]:
        assert isinstance(slot["temperature"], int)
        assert isinstance(slot["lost"], int)
        assert isinstance(slot["mode"], int)
    imu = kw["imu_state"]
    assert isinstance(imu["temperature"], int)
    bms = kw["bms_state"]
    assert isinstance(bms["soc"], int)
    assert isinstance(bms["current"], int)
    assert isinstance(bms["cycle"], int)
    assert all(isinstance(v, int) for v in bms["cell_vol"])
    assert isinstance(kw["bandwidth"], int)
    assert isinstance(kw["tick"], int)
    assert isinstance(kw["crc"], int)
    print("PASS test_all_integer_fields_are_int_not_float")


def test_nan_does_not_raise_and_clamps():
    """NaN 读数不能抛异常——`int(nan)` 会 ValueError 打断整个会话。

    丢包/失联注入会把读数写成 NaN（见 `emit.payload_for` 的注释）。
    这里断言：NaN → 量程下界，且**整个过程不抛**。
    """
    t = FakeTelemetry(joints=_joints_from_actuators())
    t.temperature = np.full(12, math.nan)
    t.lost = np.zeros(12)
    kw = build_low_state_kwargs(t)          # 不抛就算过
    assert all(s["temperature"] == 0 for s in kw["motor_state"][:12])
    print("PASS test_nan_does_not_raise_and_clamps")


def test_clamp_respects_width_and_nan():
    """`_clamp_int` 是唯一做量程夹取的地方，单独钉住它的边界。"""
    assert _clamp_int(300.0, 0, 255) == 255          # uint8 上界
    assert _clamp_int(-5.0, 0, 255) == 0             # 下界
    assert _clamp_int(math.nan, 7, 255) == 7         # NaN → lo
    assert _clamp_int(math.inf, 0, 255) == 0         # inf 也是非有限
    assert _clamp_int(42.6, 0, 255) == 43            # 四舍五入
    assert _clamp_int(0, 0, 0xFFFFFFFF) == 0
    print("PASS test_clamp_respects_width_and_nan")


def test_unknown_joint_raises_instead_of_silently_skipping():
    """关节名不在槽位表里必须抛 KeyError，**不能静默跳过**。

    静默跳过的后果：某条腿永远发 0，而界面上一切正常——最难查的那类。
    """
    t = FakeTelemetry(joints=["FL_hip", "NOT_A_JOINT"])
    t.q = np.zeros(2)
    t.dq = np.zeros(2)
    t.ddq = np.zeros(2)
    t.torque = np.zeros(2)
    t.q_raw = np.zeros(2)
    t.dq_raw = np.zeros(2)
    t.ddq_raw = np.zeros(2)
    t.temperature = np.zeros(2)
    t.lost = np.zeros(2, dtype=np.int64)
    try:
        build_low_state_kwargs(t)
    except KeyError as e:
        assert "NOT_A_JOINT" in str(e)
    else:
        raise AssertionError("未知关节名必须抛 KeyError，不能静默跳过")
    print("PASS test_unknown_joint_raises_instead_of_silently_skipping")


def test_reserved_and_unfilled_fields_are_zero_not_fabricated():
    """保留位与未建模字段一律置零。

    `crc` 置零是**诚实**的：真算 CRC 得按 SDK 的算法逐字段累加，
    本仓库没有真机可对账。置零 → 收端立刻能看出"这帧没算 CRC"。
    填个假 CRC 才是真正的错。
    """
    t = FakeTelemetry(joints=_joints_from_actuators())
    kw = build_low_state_kwargs(t)
    assert kw["crc"] == 0
    assert kw["reserve"] == 0
    assert kw["head"] == [0, 0]
    assert kw["sn"] == [0, 0]
    assert kw["level_flag"] == 0
    assert kw["wireless_remote"] == [0] * 40
    print("PASS test_reserved_and_unfilled_fields_are_zero_not_fabricated")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
