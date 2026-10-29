"""`rig.py` 里**不碰 MuJoCo** 的那部分：帧缩放、四元数、遥测取值、配置。

为什么单独成文
--------------
`rig.py` 主体是物理（`Go2Sim.__init__` 里 `import mujoco`），CI 装
`.[dev,postgres,sim]` 才能跑到。但同一个文件里还有一批**纯函数**：画面
等比裁切、四元数→旋转矩阵、部件名→关节索引、扁平指标。这些是每次渲染
和每条信封都会走的路径，出错的表现分别是"狗被压扁"和"读错关节却没人
发现"——比物理更该被钉住。

它们此前只被物理用例顺带覆盖，而物理用例在 CI 上跳过，于是 `rig.py`
在 CI 的覆盖率只有 18%。本文件让这些路径**不依赖可选依赖**也被测到。

约定同 `test_go2_sim_pure.py`：每个 test 结尾 `print("PASS <name>")`。
"""
from __future__ import annotations

import numpy as np
import pytest

from data_dog.go2_sim.rig import (
    _COMPONENT_TO_JOINT,
    _JOINT_TO_COMPONENT,
    MOTOR_TEMP_CEILING,
    TELEMETRY_HZ,
    SimConfig,
    Telemetry,
    _fit,
    _quat_to_mat,
    demo_joint,
)
from data_dog.go2_sim.subsystems import (
    BatteryState,
    ImuState,
    SystemState,
    ThermalState,
)

# ---------------------------------------------------------------- 帧缩放

def test_fit_returns_same_array_when_size_matches() -> None:
    """尺寸一致时**原样返回**，不白跑一次 LANCZOS 重采样。

    渲染每帧 ~20ms，白白重采样一次会让画面刷新率掉一截，而且
    LANCZOS 重采样同尺寸也不是恒等（边缘会轻微振铃）。
    """
    img = np.zeros((120, 200, 3), dtype=np.uint8)
    out = _fit(img, 200, 120)
    assert out is img, "同尺寸必须返回同一个对象"
    print("PASS test_fit_returns_same_array_when_size_matches")


def test_fit_crops_center_instead_of_stretching() -> None:
    """等比铺满再**居中裁切**——直接 resize 会把狗压扁/拉长。

    200×120（5:3）→ 100×100（1:1）：等比放大到 200×333 再上下各裁掉
    116 行。若实现改回 `resize((w,h))`，几何就错了，而画面看起来
    "还能看"——所以这里同时钉形状与"不是拉伸"。
    """
    img = np.zeros((120, 200, 3), dtype=np.uint8)
    img[:, :80] = 255                       # 原图左侧 40% 白
    out = _fit(img, 100, 100)
    assert out.shape == (100, 100, 3), out.shape
    # 等比放大到 167×100 后左右各裁 33 列：输出列 0 = 原图列 33（白），
    # 输出列 90 = 原图列 123（黑），边界落在输出列 47。
    # 若改成直接 `resize((100,100))`，边界会落在输出列 40——差 7 列能测出来。
    assert out[:, 0, 0].mean() == 255, "左边缘应当还是白"
    assert out[:, 90, 0].mean() == 0, "右边缘应当还是黑"
    # 高度恰好 100 无需竖直裁切：首行与末行的左边缘都还是白
    assert out[0, 0, 0] == out[-1, 0, 0] == 255
    print("PASS test_fit_crops_center_instead_of_stretching")


def test_fit_handles_upscale_and_downscale() -> None:
    img = np.full((48, 64, 3), 128, dtype=np.uint8)
    assert _fit(img, 640, 400).shape == (400, 640, 3)
    assert _fit(img, 64, 48).shape == (48, 64, 3)
    assert _fit(img, 8, 6).shape == (6, 8, 3)
    print("PASS test_fit_handles_upscale_and_downscale")


# ---------------------------------------------------------------- 四元数

def test_quat_to_mat_identity_and_degenerate() -> None:
    """单位四元数 → 单位矩阵；零四元数 → 也返回单位矩阵（不除零）。

    零四元数在物理里意味着"没有姿态"，返回 NaN 会一路污染控制力矩。
    """
    np.testing.assert_allclose(_quat_to_mat((1.0, 0.0, 0.0, 0.0)), np.eye(3),
                               atol=1e-12)
    np.testing.assert_allclose(_quat_to_mat((0.0, 0.0, 0.0, 0.0)), np.eye(3))
    print("PASS test_quat_to_mat_identity_and_degenerate")


def test_quat_to_mat_is_orthonormal_rotation() -> None:
    """任意四元数给出的必须是正交、行列式 +1 的旋转矩阵。

    这条是**结构性**的：偏航/滚转/俯仰三种各测一遍，任何一个符号写反，
    行列式会变成 −1（那就是反射，不是旋转）。
    """
    half = np.pi / 4.0
    cases = {
        "yaw": (np.cos(half), 0.0, 0.0, np.sin(half)),
        "roll": (np.cos(half), np.sin(half), 0.0, 0.0),
        "pitch": (np.cos(half), 0.0, np.sin(half), 0.0),
        "arbitrary": (0.5, 0.5, 0.5, 0.5),
    }
    for name, q in cases.items():
        m = _quat_to_mat(q)
        np.testing.assert_allclose(m @ m.T, np.eye(3), atol=1e-12,
                                   err_msg=name)
        assert np.linalg.det(m) == pytest.approx(1.0, abs=1e-12), name
    # 偏航 90°：世界 +x 轴 → +y 轴
    m = _quat_to_mat(cases["yaw"])
    np.testing.assert_allclose(m @ np.array([1.0, 0.0, 0.0]), [0.0, 1.0, 0.0],
                               atol=1e-12)
    print("PASS test_quat_to_mat_is_orthonormal_rotation")


# ---------------------------------------------------------------- 部件映射

def test_joint_component_map_is_bijective() -> None:
    """关节 ↔ 部件名必须一一对应。

    不是双向的时候 `component_row` 会静默取到另一个关节的读数，
    表现是"故障打在 A，检测器看着 B"——两条线看起来都正常，就是没候选。
    """
    assert len(_JOINT_TO_COMPONENT) == len(_COMPONENT_TO_JOINT) == 4
    for joint, comp in _JOINT_TO_COMPONENT.items():
        assert _COMPONENT_TO_JOINT[comp] == joint
    print("PASS test_joint_component_map_is_bijective")


def test_demo_joint_maps_back_to_demo_component() -> None:
    """`demo_joint()` 必须就是 `DEMO_COMPONENT` 对应的那个关节。

    所有关节类故障都定向到它；两者一旦脱钩，故障就打在没人看的数据上。
    """
    from data_dog.go2_sim.emit import DEMO_COMPONENT

    assert _JOINT_TO_COMPONENT[demo_joint()] == DEMO_COMPONENT
    print("PASS test_demo_joint_maps_back_to_demo_component")


def test_component_row_reads_the_named_joint_not_row_zero() -> None:
    """`component_row` 必须按**部件名**取行，不是一律取第 0 行。

    这里给 12 个关节各不相同的值：取错索引立刻能看出来。
    """
    t = Telemetry(
        t=1.0,
        temperature=np.arange(12, dtype=float) + 20.0,
        current=np.arange(12, dtype=float),
        torque=np.arange(12, dtype=float) * 2.0,
        q=np.arange(12, dtype=float) * 0.1,
        q_raw=np.arange(12, dtype=float) * 0.2,
        dq=np.arange(12, dtype=float) * 0.3,
        dq_raw=np.arange(12, dtype=float) * 0.4,
        valid=np.array([True] * 11 + [False]),
    )
    for joint, comp in _JOINT_TO_COMPONENT.items():
        i = t.joints.index(joint)
        row = t.component_row(comp)
        assert row is not None, comp
        assert row["temperature"] == pytest.approx(20.0 + i), comp
        assert row["current"] == pytest.approx(float(i)), comp
        assert row["tau_est"] == pytest.approx(2.0 * i), comp
        assert row["q"] == pytest.approx(0.1 * i), comp
        assert row["q_raw"] == pytest.approx(0.2 * i), comp
        assert row["dq"] == pytest.approx(0.3 * i), comp
        assert row["dq_raw"] == pytest.approx(0.4 * i), comp
        assert row["lost"] == (0.0 if i < 11 else 1.0), comp
    print("PASS test_component_row_reads_the_named_joint_not_row_zero")


def test_component_row_returns_none_for_unknown_or_missing_joint() -> None:
    """未知部件 / 该关节不在本次采样里 → None（**不是**全零行）。

    补一行零会被下游当成"读数正常"，把丢包伪装成了健康。
    """
    t = Telemetry(t=0.0)
    assert t.component_row("") is None
    assert t.component_row("battery.main") is None
    assert t.component_row("joint_motor.fl_hip") is None     # 少了前缀
    assert t.component_row("joint_motor.front_left_calf") is None  # 未映射

    # 该关节不在本次采样的 joints 列表里 → None
    t2 = Telemetry(t=0.0, joints=["FL_thigh"])
    assert t2.component_row("joint_motor.front_left_knee") is not None
    assert t2.component_row("joint_motor.front_left_hip") is None
    print("PASS test_component_row_returns_none_for_unknown_or_missing_joint")


def test_flat_metrics_flattens_subsystems_and_skips_missing_ones() -> None:
    """扁平指标：四个子系统都有的字段合并；缺的通道**不出现**（不是填 0）。

    填 0 会让"这一路没数据"变成"这一路读数 0"——正好是门禁里
    "不许把缺失伪装成正常"那条的同一种错。
    """
    empty = Telemetry(t=0.0)
    flat = empty.flat_metrics()
    assert "soc" not in flat and "roll" not in flat, flat
    assert flat["temp_max"] == 0.0
    assert flat["current_max"] == 0.0
    assert flat["valid_rate"] == 1.0
    assert "temp_max" in flat and "current_max" in flat and "valid_rate" in flat

    full = Telemetry(
        t=1.0,
        temperature=np.array([30.0] * 11 + [90.0]),
        current=np.array([1.0] * 11 + [4.5]),
        valid=np.array([True] * 11 + [False]),
        battery=BatteryState(soc=77.0),
        imu=ImuState(), thermal=ThermalState(), system=SystemState(),
    )
    flat = full.flat_metrics()
    assert flat["soc"] == pytest.approx(77.0)
    assert flat["temp_max"] == pytest.approx(90.0)
    assert flat["current_max"] == pytest.approx(4.5)
    assert flat["valid_rate"] == pytest.approx(11 / 12, abs=1e-4)
    assert "power_a" in flat and "roll" in flat and "fan_frequency" in flat
    print("PASS test_flat_metrics_flattens_subsystems_and_skips_missing_ones")


# ---------------------------------------------------------------- 常量与配置

def test_motor_temp_ceiling_is_int8_max() -> None:
    """温度上界 127°C 来自厂商 IDL 的 `int8_t`，不是拍出来的。

    `motor_msg_GO-M8010-6.h` 第 45 行 `int8_t temperature`：127 是能表达的
    最大正整数。仿真读数超过它就是在发真机发不出来的数——下游按 IDL
    声明字段类型时会被截断成负数（−128），温度曲线瞬间"变冷"。
    """
    assert MOTOR_TEMP_CEILING == 127.0
    assert int(MOTOR_TEMP_CEILING) == np.iinfo(np.int8).max
    print("PASS test_motor_temp_ceiling_is_int8_max")


def test_telemetry_rate_matches_physics_substeps() -> None:
    """100Hz 遥测 = 2ms 物理步 × 5。改任一个都要同步改另一个。"""
    assert TELEMETRY_HZ == 100.0
    assert pytest.approx(5.0) == 1.0 / (TELEMETRY_HZ * 0.002)
    print("PASS test_telemetry_rate_matches_physics_substeps")


def test_sim_config_defaults_and_resolved_seed() -> None:
    """默认值本身是契约：姿态 stand、故障 normal、severity 1.0、热缩放 1.0。

    `severity` 默认 1.0 很重要——0.0 意味着"故障强度为零"，
    演示会安静地什么都不报。`thermal_scale=1.0` 是**物理原值**，
    UI 上开大于 1 必须显示"非物理加速"。
    """
    cfg = SimConfig()
    assert (cfg.posture, cfg.fault, cfg.severity) == ("stand", "normal", 1.0)
    assert cfg.thermal_scale == 1.0
    assert cfg.seed is None and cfg.fault_duration_s is None
    assert cfg.fault_start_s == 8.0, "开局的健康基线长度是契约"

    assert SimConfig(seed=7).resolved_seed() == 7
    assert SimConfig(seed=0).resolved_seed() == 0        # 0 是合法 seed，不是"未给"
    # 未给 seed 时取当前纳秒并截到 31 位正数（下游按 uint32 序列化）。
    # 注意：Windows 上 `time.time_ns()` 实测只有 ~1ms 分辨率，
    # 同毫秒内构造的两个实例会拿到同一个 seed——这里只钉范围，不钉唯一性。
    for _ in range(5):
        got = SimConfig().resolved_seed()
        assert 0 <= got <= 0x7FFFFFFF, got
    print("PASS test_sim_config_defaults_and_resolved_seed")
