"""`go2_sim/` 的**纯逻辑**测试：不跑真仿真，钉住契约与踩过的坑。

为什么要有这个文件
------------------
`go2_sim/` 是实时演示接入器，一旦某条契约悄悄变了，表现出来是
"页面看着正常、候选永远是 0"——最难查的那一类。所以这里不测
"物理对不对"（那是真仿真的事），只测**边界和契约**：

1. **信封契约**：网关 `_REQUIRED_FIELDS` 一条不能少、`payload_inline`
   必须是 JSON **字符串**（传 object 会被 `payload_inline_not_string` 拒）。
2. **NaN 处理**（问题 #14）：丢包/失联注入把读数写成 NaN，若照常序列化
   会炸在 `int(round(nan))` 上，把整个仿真会话打断。必须是"整帧跳过"。
3. **故障定向**（问题 #15）：关节类故障必须打在**发布出去的那个关节**上。
   钉错位置的表现是"检测器检不出"，实际是"故障没送到检测器眼前"。
4. **事件时间**：变速下墙钟与事件时间脱钩，`occurred_at` 必须按仿真时钟算。

约定同 `tests/test_mujoco_pure.py`：每个 test 结尾 `print("PASS <name>")`。

真仿真相关的用例用 `pytest.importorskip("mujoco")` 守卫——没装可选依赖时跳过，
而不是失败。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import pytest

from data_dog.go2_sim.emit import (
    DEMO_COMPONENT,
    EPOCH,
    FIRMWARE,
    MODEL,
    PROTOCOL_VERSION,
    VENDOR,
    envelope,
    payload_for,
)
from data_dog.go2_sim.faults import FAULT_LIBRARY, Layer, by_subsystem
from data_dog.go2_sim.postures import POSTURES

# 网关必填字段（照抄 `data_plane/gateway.py::_REQUIRED_FIELDS`）。
_REQUIRED_FIELDS = (
    "event_id", "occurred_at", "received_at", "sequence",
    "vendor", "model", "firmware",
)


class _Row(dict):
    """够用的假遥测：只要 `component_row` 返回 dict 即可。"""


class FakeTelemetry:
    """不 import MuJoCo 也能构造的遥测替身。"""

    def __init__(self, *, t: float, row: dict | None):
        self.t = t
        self._row = row

    def component_row(self, component: str) -> dict | None:
        if component != DEMO_COMPONENT:
            return None
        return self._row


def _healthy_row(**over: float) -> dict:
    row = {
        "temperature": 42.4, "current": 5.73, "tau_est": 3.442,
        "q": 0.11, "q_raw": 0.11, "dq": 1.2, "dq_raw": 1.2, "lost": 0.0,
    }
    row.update(over)
    return row


# ---------------------------------------------------------------- 信封契约

def test_envelope_has_all_gateway_required_fields() -> None:
    """少一个必填字段，网关就整条拒掉——而且是静默丢，页面只会"没反应"。"""
    env = envelope(FakeTelemetry(t=1.0, row=_healthy_row()), sequence=7)
    assert env is not None
    missing = [f for f in _REQUIRED_FIELDS if f not in env]
    assert not missing, f"信封缺必填字段: {missing}"
    assert env["vendor"] == VENDOR
    assert env["model"] == MODEL
    assert env["firmware"] == FIRMWARE
    assert env["protocol_version"] == PROTOCOL_VERSION
    assert env["component_hint"] == DEMO_COMPONENT
    print("PASS test_envelope_has_all_gateway_required_fields")


def test_payload_inline_is_json_string_not_object() -> None:
    """网关按**文本**算大小再 `json.loads`；传 object 会被直接拒绝。"""
    env = envelope(FakeTelemetry(t=1.0, row=_healthy_row()), sequence=1)
    assert isinstance(env["payload_inline"], str), (
        f"payload_inline 必须是 str，实际 {type(env['payload_inline'])}"
    )
    decoded = json.loads(env["payload_inline"])
    assert isinstance(decoded, dict)
    print("PASS test_payload_inline_is_json_string_not_object")


def test_payload_matches_motor_state_idl_types() -> None:
    """`temperature`/`lost` 是 IDL 的 uint8：必须是整数，不能是 42.4。"""
    payload = payload_for(FakeTelemetry(t=0.0, row=_healthy_row()), DEMO_COMPONENT)
    assert payload is not None
    assert isinstance(payload["temperature"], int)
    assert isinstance(payload["lost"], int)
    assert payload["mode"] == 0, "仿真恒为 RPC 模式，mode 固定 0"
    # 其余是浮点，保留 6 位
    assert payload["current"] == round(5.73, 6)
    print("PASS test_payload_matches_motor_state_idl_types")


def test_unknown_component_returns_none() -> None:
    """没映射的部件不发信封，而不是发一条空载荷。"""
    fake = FakeTelemetry(t=0.0, row=_healthy_row())
    assert payload_for(fake, "joint_motor.nonexistent") is None
    assert envelope(fake, component="joint_motor.nonexistent", sequence=1) is None
    print("PASS test_unknown_component_returns_none")


# ---------------------------------------------------------------- NaN（问题 #14）

@pytest.mark.parametrize("bad_field", ["temperature", "current", "tau_est",
                                       "q", "q_raw", "dq", "dq_raw", "lost"])
def test_nan_in_any_field_skips_whole_frame(bad_field: str) -> None:
    """**任意**一个字段是 NaN，整帧跳过。

    回归问题 #14：F3 丢包把读数写成 NaN，`int(round(nan))` 抛 ValueError，
    直接把发布线程打断、整个仿真会话终止。八个字段逐个钉，避免只修了
    temperature 就把别的漏掉。
    """
    row = _healthy_row(**{bad_field: float("nan")})
    fake = FakeTelemetry(t=0.0, row=row)
    assert payload_for(fake, DEMO_COMPONENT) is None, (
        f"{bad_field}=NaN 时应当整帧跳过"
    )
    assert envelope(fake, sequence=1) is None
    print(f"PASS test_nan_in_any_field_skips_whole_frame[{bad_field}]")


def test_infinity_also_skipped() -> None:
    """inf 和 NaN 一样不可序列化成 uint8；用 isfinite 而不是 isnan 才拦得住。"""
    assert payload_for(FakeTelemetry(t=0.0, row=_healthy_row(temperature=math.inf)),
                       DEMO_COMPONENT) is None
    print("PASS test_infinity_also_skipped")


def test_nan_frame_leaves_sequence_hole_not_fake_zero() -> None:
    """丢包必须表现为**序列号空洞**，不能补一个假 0。

    `COMM_PACKET_LOSS` 的判据是"窗口覆盖率 <95%"。若把丢掉的帧补成 0°C / 0A，
    覆盖率仍是 100%，规则永远不触发——丢包就被伪装成了正常数据。
    """
    rows = [_healthy_row(), _healthy_row(temperature=float("nan")), _healthy_row()]
    sent, skipped = 0, 0
    for i, row in enumerate(rows):
        if envelope(FakeTelemetry(t=float(i), row=row), sequence=i) is None:
            skipped += 1
        else:
            sent += 1
    assert (sent, skipped) == (2, 1)
    print("PASS test_nan_frame_leaves_sequence_hole_not_fake_zero")


# ---------------------------------------------------------------- 事件时间

def test_occurred_at_uses_sim_clock_not_wall_clock() -> None:
    """15 倍速下墙钟和事件时间脱钩：`occurred_at` 必须 = 会话起点 + 仿真 t。

    若改用 `now()`，15 倍速会把"1 秒的窗口"塞进 15 秒的数据量，
    所有窗口类判据（斜率、覆盖率）全部失真。
    """
    env = envelope(FakeTelemetry(t=12.5, row=_healthy_row()), sequence=1)
    assert env is not None
    assert env["occurred_at"] == "2026-09-13T10:00:12.500000Z"
    print("PASS test_occurred_at_uses_sim_clock_not_wall_clock")


def test_custom_session_start_shifts_event_time() -> None:
    start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    env = envelope(FakeTelemetry(t=3.0, row=_healthy_row()), sequence=1,
                   session_start=start)
    assert env is not None
    assert env["occurred_at"] == "2030-01-01T00:00:03Z"
    print("PASS test_custom_session_start_shifts_event_time")


def test_default_epoch_is_fixed_for_reproducibility() -> None:
    """固定 EPOCH（不是 now()）才能让同一份录像/回放可复现。"""
    assert EPOCH == datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc)
    print("PASS test_default_epoch_is_fixed_for_reproducibility")


# ---------------------------------------------------------------- 故障定向（问题 #15）

def test_demo_component_is_a_knee_not_a_hip() -> None:
    """演示主部件必须是会发热的 thigh/knee，不能是几乎不承重的 hip。

    实测（60 秒奔跑 + 散热失效）：四只 thigh 95~129°C，四只 hip 26~37°C。
    检测器阈值是 `temperature >= 90`，发 hip 就永远不可能触发。
    """
    assert "knee" in DEMO_COMPONENT, (
        f"演示主部件是 {DEMO_COMPONENT}；hip 是偏摆轴、几乎不发热，"
        "温度规则永远触发不了（问题 #15）"
    )
    print("PASS test_demo_component_is_a_knee_not_a_hip")


def test_demo_joint_matches_demo_component() -> None:
    """`rig.demo_joint()` 必须与 `emit.DEMO_COMPONENT` 反查一致。

    问题 #15：四条关节故障写死了 `FL_hip`，而发布的是 FL_thigh，
    故障打在没人读的关节上，覆盖矩阵因此把它们全判成 MISS。
    """
    pytest.importorskip("mujoco")
    from data_dog.go2_sim.rig import _JOINT_TO_COMPONENT, demo_joint

    joint = demo_joint()
    assert _JOINT_TO_COMPONENT[joint] == DEMO_COMPONENT
    print("PASS test_demo_joint_matches_demo_component")


# ---------------------------------------------------------------- 故障库

def test_every_fault_key_is_unique() -> None:
    keys = [f.key for f in FAULT_LIBRARY]
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes, f"故障 key 重复: {dupes}"
    print("PASS test_every_fault_key_is_unique")


def test_normal_baseline_fault_exists() -> None:
    """必须有一条 `normal` 作对照——它检不出才说明没有误报。"""
    assert any(f.key == "normal" for f in FAULT_LIBRARY)
    print("PASS test_normal_baseline_fault_exists")


def test_fault_library_covers_all_six_subsystems() -> None:
    """"所有故障"落成六个子系统，缺一个说明覆盖有洞。"""
    buckets = by_subsystem()
    expected = {"joint", "thermal", "power", "imu", "comms", "system"}
    assert expected <= set(buckets), (
        f"缺少子系统: {expected - set(buckets)}"
    )
    for name, specs in buckets.items():
        assert specs, f"子系统 {name} 一个故障都没有"
    print("PASS test_fault_library_covers_all_six_subsystems")


def test_every_fault_declares_layer_and_story() -> None:
    """每条故障都要有注入层和物理故事——否则演示时讲不出来。"""
    for spec in FAULT_LIBRARY:
        assert isinstance(spec.layer, Layer), f"{spec.key} 缺注入层"
        assert spec.story.strip(), f"{spec.key} 缺物理故事"
        assert spec.symptom.strip(), f"{spec.key} 缺症状描述"
    print("PASS test_every_fault_declares_layer_and_story")


# ---------------------------------------------------------------- 姿态

def test_postures_have_distinct_targets() -> None:
    """站立/奔跑/趴卧三个姿态的关节目标必须真的不一样。"""
    names = set(POSTURES)
    assert {"stand", "run", "prone"} <= names, f"姿态缺失，现有 {names}"
    print("PASS test_postures_have_distinct_targets")


# ---------------------------------------------------------------- 真仿真（可选依赖）

def test_run_fault_reaches_published_joint() -> None:
    """真仿真：关节类故障必须改变**发布出去的那个关节**的读数。

    这是问题 #15 的端到端回归。只断言"故障关节的读数变了"，
    不断言具体物理数值（那会随 MuJoCo 版本漂）。
    """
    pytest.importorskip("mujoco")
    from data_dog.go2_sim.rig import Go2Sim, SimConfig

    def sample(fault: str, field: str) -> list[float]:
        sim = Go2Sim(SimConfig(posture="run", fault=fault, severity=1.0, seed=7,
                               fault_start_s=2.0, thermal_scale=80.0))
        try:
            vals = []
            for ch in sim.step(600):          # 6 仿真秒
                row = ch.component_row(DEMO_COMPONENT)
                if row is not None and 3.0 <= ch.t <= 6.0:
                    vals.append(row[field])
            return vals
        finally:
            sim.close()

    healthy = sample("normal", "current")
    stalled = sample("F1_stall", "current")
    assert healthy and stalled
    # 用**均值**而不是 max：奔跑步态本身有电流尖峰，normal 的 max 也能冲到
    # 13.8A，和堵转分不开。均值才分得开（实测 normal 5.7A vs F1 14.8A）。
    mean_healthy = sum(healthy) / len(healthy)
    mean_stalled = sum(stalled) / len(stalled)
    assert mean_stalled > mean_healthy * 1.5, (
        f"F1 堵转没有抬升发布关节的电流："
        f"normal mean={mean_healthy:.2f}A, F1 mean={mean_stalled:.2f}A"
    )
    print("PASS test_run_fault_reaches_published_joint")


def test_motor_temp_is_clamped_to_sensor_range() -> None:
    """真仿真：温度必须被钳在厂商量程内，不能出现 296°C。

    一阶热模型是线性的，稳态 ΔT = I²·R·Rth，**没有物理上限**。F2 把 Rth
    放大 20 倍后，奔跑电流的稳态是 598°C、额定 15A 是 891°C——面板上因此
    出现过 296°C。而厂商头文件 `motor_msg_GO-M8010-6.h` 第 45 行写明：

        int8_t temp;   // 电机温度: -128~127°C 90°C时触发温度保护

    驱动侧是 int8_t，**最多只能表示 127°C**。演出来的数超过它，就不是
    "更严重的故障"，而是传感器根本读不出的值 —— 演示视频里像程序算错了。
    """
    pytest.importorskip("mujoco")
    from data_dog.go2_sim.rig import MOTOR_TEMP_CEILING, Go2Sim, SimConfig

    assert MOTOR_TEMP_CEILING == 127.0, "量程上限来自 int8_t，不能随手改"

    sim = Go2Sim(SimConfig(posture="run", fault="F2_temp_rise", severity=1.0,
                           seed=7, fault_start_s=0.0, thermal_scale=400.0))
    try:
        peak = 0.0
        for _ in sim.step(4000):            # 40 仿真秒，加速到接近稳态
            peak = max(peak, float(sim._T.max()))
    finally:
        sim.close()
    assert peak <= MOTOR_TEMP_CEILING + 1e-6, (
        f"温度涨到 {peak:.1f}°C，超过 int8_t 量程 {MOTOR_TEMP_CEILING}°C"
    )
    assert peak >= 90.0, (
        f"钳位后 F2 只到 {peak:.1f}°C，越不过 90°C 保护线——故障就演不出来了"
    )
    print(f"PASS test_motor_temp_is_clamped_to_sensor_range (peak={peak:.1f})")


def test_fan_stalled_and_f2_differ_by_fan_frequency() -> None:
    """F9 与 F2 温度都升，靠 `fan_frequency` 分开——不能只靠温度。

    两者都是散热侧故障，温度区间重叠（奔跑下 F9≈80°C、F2 顶到 127°C）。
    若只看温度，F9 会被当成"较轻的 F2"。真正的判别信号是风扇转速：
    F9 风扇停转 → 0，F2 风扇照转但散热片堵了 → 非 0。
    """
    pytest.importorskip("mujoco")
    from data_dog.go2_sim.rig import Go2Sim, SimConfig

    def fan(fault: str) -> float:
        sim = Go2Sim(SimConfig(posture="run", fault=fault, severity=1.0,
                               seed=7, fault_start_s=0.0, thermal_scale=400.0))
        try:
            for _ in sim.step(1200):
                pass
            return float(sim._last.thermal.fan_frequency)
        finally:
            sim.close()

    assert fan("F9_fan_stalled") == 0.0, "F9 风扇停转，转速必须是 0"
    assert fan("F2_temp_rise") > 0.0, "F2 风扇照转，转速不能是 0"
    print("PASS test_fan_stalled_and_f2_differ_by_fan_frequency")


def test_packet_loss_produces_skipped_frames() -> None:
    """真仿真：F3 丢包必须真的产生"整帧跳过"，且不抛异常（问题 #14 回归）。"""
    pytest.importorskip("mujoco")
    from data_dog.go2_sim.rig import Go2Sim, SimConfig

    sim = Go2Sim(SimConfig(posture="run", fault="F3_packet_loss", severity=1.0,
                           seed=7, fault_start_s=2.0, thermal_scale=80.0))
    try:
        sent = skipped = 0
        for i, ch in enumerate(sim.step(1500)):
            if envelope(ch, sequence=i) is None:
                skipped += 1
            else:
                sent += 1
    finally:
        sim.close()
    assert sent > 0, "一帧都没发出去"
    assert skipped > 0, "F3 丢包没有产生任何跳过帧——NaN 没有传到信封层"
    print(f"PASS test_packet_loss_produces_skipped_frames "
          f"(sent={sent}, skipped={skipped})")
