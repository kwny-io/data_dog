"""仿真编排层：会话循环、MQTT 出口、控制台 HTTP 端点。

为什么单独一个文件
------------------
`test_go2_sim_pure.py` 钉物理与编码契约、`test_go2_sim_subsystems.py` 钉
子系统那条数据面。本文件钉的是**把仿真器变成"能跑的演示"的那三层**，
它们此前几乎零覆盖（session 20%、mqtt_pub 23.5%、server 47.8%），
而这三层恰恰是"改一行就静默失灵"的地方：

- **session**：变速/暂停/复位的时间基准换算，投递抽样，失败计数。
  这里的每个数都是**墙钟 vs 仿真时钟**的换算；换错不会报错，只会
  让页面上的"实际倍率"变成假的、或者让投递频率在 15x 下打穿网关限流。
- **mqtt_pub**：`enabled=False` 的**空实现**语义。演示不能因为 broker
  没起就停，但也不能假装发出去了——`last_error` 必须说清楚为什么没发。
- **server**：控制台端点。`_telemetry_rows` 会被 200ms 一次的轮询直接
  消费，结构错了页面就是白屏。

**全部不需要 MuJoCo**：`session.Go2Sim` 换成替身、`server.SimSession`
换成替身即可——这三层的逻辑本来就与物理引擎无关。这样才能进 CI
（CI 装 `.[dev,postgres]`，不装 `sim` extra）。

约定同 `test_go2_sim_pure.py`：每个 test 结尾 `print("PASS <name>")`。
"""
from __future__ import annotations

import io
import json
import sys
import time
import types
from typing import Any

import numpy as np
import pytest

from data_dog.go2_sim import server as server_mod
from data_dog.go2_sim import session as session_mod
from data_dog.go2_sim.mqtt_pub import MqttTelemetryPublisher
from data_dog.go2_sim.rig import SimConfig, Telemetry
from data_dog.go2_sim.server import _PlaneSink, _telemetry_rows
from data_dog.go2_sim.session import (
    SPEEDS,
    SessionStats,
    SimEvent,
    SimSession,
)
from data_dog.go2_sim.subsystems import (
    BatteryState,
    ImuState,
    SystemState,
    ThermalState,
)

# ---------------------------------------------------------------- 替身

def _jpg(width: int = 200, height: int = 120) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(buf, "JPEG")
    return buf.getvalue()


def _telemetry(t: float, *, temp: float = 30.0) -> Telemetry:
    """一条够用的遥测：12 关节 + 四个子系统（子系统是信封数的前提）。"""
    return Telemetry(
        t=t,
        temperature=np.full(12, temp),
        current=np.full(12, 2.0),
        torque=np.full(12, 3.0),
        battery=BatteryState(), imu=ImuState(),
        thermal=ThermalState(), system=SystemState(),
    )


class _FakeSim:
    """`Go2Sim` 的替身：只实现会话层真正用到的那 7 个方法。

    会话层与物理引擎的接口就这么窄（step / snapshot / reset / inject /
    render_jpeg / close / config），换成替身不需要动被测代码。
    """

    OFFSCREEN_W = 1280
    OFFSCREEN_H = 800

    def __init__(self, config: Any) -> None:
        self.config = config
        self.t = 0.0
        self.closed = 0
        self.steps = 0
        self.injected: list[tuple[str, dict]] = []
        self.resets: list[Any] = []
        self.render_calls = 0
        self.step_sleep = 0.0
        self.render_error: Exception | None = None

    def step(self, n: int) -> list[Telemetry]:
        if self.step_sleep:
            time.sleep(self.step_sleep)
        out = []
        for _ in range(n):
            self.t += 0.01
            out.append(_telemetry(self.t))
        self.steps += n
        return out

    def reset(self, config: Any = None) -> None:
        self.resets.append(config)
        if config is not None:
            self.config = config
        self.t = 0.0

    def inject(self, key: str, **kw: Any) -> None:
        self.injected.append((key, kw))

    def snapshot(self) -> dict:
        return {"t": self.t}

    def render_jpeg(self, width: int = 640, height: int = 400) -> bytes:
        self.render_calls += 1
        if self.render_error is not None:
            raise self.render_error
        return _jpg(64, 48)

    def close(self) -> None:
        self.closed += 1


@pytest.fixture()
def fake_rig(monkeypatch):
    """把 `session.Go2Sim` 换成替身，并让调用方能拿到构造出来的那个实例。

    工厂函数要带上 `OFFSCREEN_W/H` 两个类属性——`set_frame_size` 是拿
    `Go2Sim.OFFSCREEN_*` 做上限的，而它走的是模块级名字（不是 `self.sim`）。
    """
    made: list[_FakeSim] = []

    def _factory(config):
        sim = _FakeSim(config)
        made.append(sim)
        return sim

    _factory.OFFSCREEN_W = _FakeSim.OFFSCREEN_W
    _factory.OFFSCREEN_H = _FakeSim.OFFSCREEN_H
    monkeypatch.setattr(session_mod, "Go2Sim", _factory)

    def _build(config, **kw):
        sess = SimSession(config, **kw)
        return sess, made[-1]

    return _build


def _sleep_until(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


# ---------------------------------------------------------------- 会话：控制面

def test_defaults_and_speed_clamping(fake_rig) -> None:
    """倍率必须被夹在 0.1–100。

    不夹的话 `1/publish_hz` 之外的另一个除法 `sim_done / speed` 会除零，
    而 0.0 是 UI 滑块的合法最小值——不是假想输入。
    """
    sess, _sim = fake_rig(SimConfig(posture="stand"))
    assert SPEEDS == (1.0, 4.0, 15.0), SPEEDS
    sess.set_speed(1000.0)
    assert sess.speed == 100.0
    sess.set_speed(0.0)
    assert sess.speed == 0.1
    sess.set_speed(-5.0)
    assert sess.speed == 0.1
    sess.set_speed(4.0)
    assert sess.speed == 4.0
    print("PASS test_defaults_and_speed_clamping")


def test_publish_hz_has_floor(fake_rig) -> None:
    """`publish_hz` 下限 0.5 → `_pub_every` 有界，不会出现整除 0。"""
    sess, _ = fake_rig(SimConfig(posture="stand"), publish_hz=0.0)
    assert sess.publish_hz == 0.5
    assert sess._pub_every == 200
    print("PASS test_publish_hz_has_floor")


def test_frame_size_clamped_to_offscreen_bounds(fake_rig) -> None:
    """请求尺寸夹到 64..1280 / 48..800。

    上限来自离屏缓冲本身（`Go2Sim.OFFSCREEN_W/H`）：超过就渲染不出来，
    而不是"渲得小一点"。
    """
    sess, _ = fake_rig(SimConfig(posture="stand"))
    sess.set_frame_size(1, 1)
    assert sess._frame_size == (64, 48)
    sess.set_frame_size(99999, 99999)
    assert sess._frame_size == (1280, 800)
    sess.set_frame_size(880, 550)
    assert sess._frame_size == (880, 550)
    print("PASS test_frame_size_clamped_to_offscreen_bounds")


def test_inject_is_forwarded_under_lock(fake_rig) -> None:
    sess, sim = fake_rig(SimConfig(posture="stand"))
    sess.inject("F1_stall", severity=0.7, duration_s=5.0, start_s=1.0)
    assert sim.injected == [("F1_stall", {"severity": 0.7, "duration_s": 5.0,
                                         "start_s": 1.0})], sim.injected
    print("PASS test_inject_is_forwarded_under_lock")


def test_running_is_false_before_start(fake_rig) -> None:
    sess, _ = fake_rig(SimConfig(posture="stand"))
    assert sess.running is False
    print("PASS test_running_is_false_before_start")


# ---------------------------------------------------------------- 会话：换算

def test_chunk_index_maps_wall_slot_back_to_telemetry_point(fake_rig) -> None:
    """墙钟格子 → 这一批里的第几个遥测点。

    这是整个投递循环的核心不变量 `sim_t = (wall - t0) * speed` 的逆运算。
    全部取批尾（`n-1`）会让 15x 下所有信封挤在同一小段时间里，
    窗口聚合看到的 t 就没有区分度。
    """
    sess, _ = fake_rig(SimConfig(posture="stand"))

    # 1x：格子 0.10s、批起点 0.00s、批长 50 点 → 第 10 个点
    sess.speed = 1.0
    assert sess._chunk_index_at(0.10, 0.0, 50, 0.0) == 10

    # 15x：同样 0.10s 墙钟推进 1.5 仿真秒 → 150 点，但批只有 50 点，夹到批尾
    sess.speed = 15.0
    assert sess._chunk_index_at(0.10, 0.0, 50, 0.0) == 49

    # 15x：0.02s 墙钟 = 0.3 仿真秒 → 30 点，在批内
    assert sess._chunk_index_at(0.02, 0.0, 50, 0.0) == 30

    # 格子早于批起点（回绕/复位后的第一格）→ 夹到 0，不能是负数索引
    assert sess._chunk_index_at(0.0, 0.5, 50, 0.0) == 0

    # 单点批：无论怎么算都只能是 0
    assert sess._chunk_index_at(10.0, 0.0, 1, 0.0) == 0
    print("PASS test_chunk_index_maps_wall_slot_back_to_telemetry_point")


def test_snapshot_actual_speed_is_none_during_warmup(fake_rig) -> None:
    """实际倍率在墙钟 <0.5s 时给 None，不拿 0 冒充。

    刚启动时 `t/wall` 是两个噪声之比（可能是 37x）。页面据此显示"—"，
    半秒后才有意义。
    """
    sess, sim = fake_rig(SimConfig(posture="stand"))
    snap = sess.snapshot()
    assert snap["actual_speed"] is None, snap
    assert snap["speed"] == 1.0
    assert snap["running"] is False
    assert snap["stats"]["wall_elapsed"] == 0 or snap["stats"]["wall_elapsed"] >= 0

    # 把基准前移 2 秒，仿真推进 4 秒 → 2.0x
    sim.t = 4.0
    sess._t0 -= 2.0
    snap = sess.snapshot()
    assert snap["actual_speed"] == pytest.approx(2.0, abs=0.05), snap
    print("PASS test_snapshot_actual_speed_is_none_during_warmup")


def test_history_returns_last_n(fake_rig) -> None:
    sess, _ = fake_rig(SimConfig(posture="stand"), history=5)
    sess._hist.extend(_telemetry(i * 0.01) for i in range(3))
    assert [round(x.t, 2) for x in sess.history(10)] == [0.0, 0.01, 0.02]
    assert len(sess.history(2)) == 2
    assert sess.history(0)[0].t == pytest.approx(0.02)   # 下限 1 条，不是空
    print("PASS test_history_returns_last_n")


# ---------------------------------------------------------------- 会话：投递

def test_publish_sends_one_motor_plus_four_subsystem_envelopes(fake_rig) -> None:
    """一个遥测点 = 5 条信封（1 电机 + 4 子系统）。

    少发子系统那 4 条，电池/IMU/散热/整机的 10 条故障永远不可能被报出
    ——不是规则没写，是数据没离开仿真器。
    """
    got: list[SimEvent] = []
    sess, _ = fake_rig(SimConfig(posture="stand"),
                       on_event=got.append)
    sess._seq = 7
    sess._publish(_telemetry(1.25))
    hints = [e.env["component_hint"] for e in got]
    assert len(hints) == 5, hints
    assert hints[0] == "joint_motor.front_left_knee", hints
    assert set(hints[1:]) == {"battery.main", "imu.body",
                              "thermal.main", "system.body"}, hints
    assert all(e.env["sequence"] == 7 for e in got)
    assert all(e.t == pytest.approx(1.25) for e in got)
    assert sess.stats.published == 5
    assert sess.stats.dropped == 0
    print("PASS test_publish_sends_one_motor_plus_four_subsystem_envelopes")


def test_publish_records_first_and_every_hundredth_drop(fake_rig) -> None:
    """投递失败必须留下**原因**，且不拖死仿真。

    这里曾经只 `dropped += 1`，什么原因都不留：页面照常跑、候选一直是 0，
    "多少帧在哪一步消失"完全看不见。记首个原因 + 每 100 次刷新一次计数。
    """
    calls = {"n": 0}

    def boom(_ev: SimEvent) -> None:
        calls["n"] += 1
        raise RuntimeError(f"下游炸了 #{calls['n']}")

    sess, _ = fake_rig(SimConfig(posture="stand"), on_event=boom)
    sess._publish(_telemetry(0.5))
    assert sess.stats.dropped == 1
    assert sess.stats.published == 0
    assert sess.stats.drop_reason is not None
    assert "RuntimeError" in sess.stats.drop_reason
    assert "共 1 帧" in sess.stats.drop_reason
    first = sess.stats.drop_reason

    # 第 2 次不刷新原因（只在 1、100、200… 刷新）
    sess._publish(_telemetry(0.6))
    assert sess.stats.dropped == 2
    assert sess.stats.drop_reason == first

    # 补到 100 次，原因带上新的总数
    for i in range(3, 101):
        sess._publish(_telemetry(0.6 + i * 0.01))
    assert sess.stats.dropped == 100
    assert "共 100 帧" in sess.stats.drop_reason
    print("PASS test_publish_records_first_and_every_hundredth_drop")


def test_publish_without_callback_is_a_noop(fake_rig) -> None:
    sess, _ = fake_rig(SimConfig(posture="stand"))
    sess._publish(_telemetry(0.0))
    assert sess.stats.published == 0 and sess.stats.dropped == 0
    print("PASS test_publish_without_callback_is_a_noop")


def test_publish_skips_row_that_is_not_finite(fake_rig) -> None:
    """整行非有限时电机那条不发，但**不算投递失败**。

    丢包时读数是 NaN（"没有读数"，不是"读数为 0"）。跳过这一帧让下游
    看到序列号空洞——那正是真实丢包的样子。把它计入 dropped 会把
    "正常的丢包注入"污染成"投递链路坏了"。
    """
    got: list[SimEvent] = []
    sess, _ = fake_rig(SimConfig(posture="stand"),
                       on_event=got.append)
    bad = _telemetry(0.3)
    bad.temperature = np.full(12, np.nan)
    sess._publish(bad)
    assert all(e.env["component_hint"] != "joint_motor.front_left_knee"
               for e in got)
    assert len(got) == 4, [e.env["component_hint"] for e in got]
    assert sess.stats.dropped == 0
    print("PASS test_publish_skips_row_that_is_not_finite")


# ---------------------------------------------------------------- 会话：画面

def test_frame_jpeg_returns_cached_bytes_when_no_size_given(fake_rig) -> None:
    sess, _ = fake_rig(SimConfig(posture="stand"))
    data = _jpg()
    sess._frame = data
    assert sess.frame_jpeg() == data
    print("PASS test_frame_jpeg_returns_cached_bytes_when_no_size_given")


def test_frame_jpeg_scales_to_requested_size(fake_rig) -> None:
    """请求尺寸与缓存帧不同就在本线程用 PIL 缩放（不碰 GL）。

    渲染只能在仿真线程做（GL 上下文绑线程），但缩放是纯像素操作——
    所以 HTTP 线程可以自己缩，代价只是几毫秒。
    """
    from PIL import Image
    sess, _ = fake_rig(SimConfig(posture="stand"))
    sess._frame = _jpg(200, 120)
    out = sess.frame_jpeg(120, 80)
    assert out != sess._frame
    im = Image.open(io.BytesIO(out))
    assert im.size == (120, 80), im.size
    # 尺寸被记为下次渲染目标
    assert sess._frame_size == (120, 80)
    print("PASS test_frame_jpeg_scales_to_requested_size")


def test_frame_jpeg_returns_raw_when_scaling_fails(fake_rig) -> None:
    """缩放失败宁可给原帧，也不能报错——画面糊一点比白屏好。"""
    sess, _ = fake_rig(SimConfig(posture="stand"))
    sess._frame = b"not-a-jpeg"
    out = sess.frame_jpeg(120, 80)
    assert out == b"not-a-jpeg"
    print("PASS test_frame_jpeg_returns_raw_when_scaling_fails")


def test_frame_state_exposes_error_and_hz(fake_rig) -> None:
    sess, _ = fake_rig(SimConfig(posture="stand"), frame_hz=5.0)
    st = sess.frame_state()
    assert st == {"t": 0.0, "bytes": 0, "size": [880, 550],
                  "error": None, "hz": 5.0}, st
    print("PASS test_frame_state_exposes_error_and_hz")


def test_render_failure_disables_rendering_instead_of_killing_session(
        fake_rig) -> None:
    """没有 GL 的机器上渲染会抛；记一次原因后**永久降为 0Hz**。

    重试会让每帧都抛一次异常，把仿真线程整个堵住（渲染约 20ms/帧，
    异常路径还要走 traceback 构造）。宁可没有画面，也不能没有仿真。
    """
    sess, sim = fake_rig(SimConfig(posture="stand"), frame_hz=1000.0)
    sim.render_error = RuntimeError("no GL context")
    sess._render_if_due()
    st = sess.frame_state()
    assert "RuntimeError: no GL context" in st["error"], st
    assert st["hz"] == 0.0, st

    sess._render_if_due()
    assert sim.render_calls == 1, sim.render_calls
    print("PASS test_render_failure_disables_rendering_instead_of_killing_session")


def test_render_is_throttled_by_wall_clock(fake_rig) -> None:
    """frame_hz 是**墙钟**频率；一帧画完立刻再调不该再渲。"""
    sess, sim = fake_rig(SimConfig(posture="stand"), frame_hz=1.0)
    sess._render_if_due()
    assert sim.render_calls == 1
    sess._render_if_due()
    assert sim.render_calls == 1, "同一秒内不该再渲染"
    sess._last_render -= 2.0
    sess._render_if_due()
    assert sim.render_calls == 2
    print("PASS test_render_is_throttled_by_wall_clock")


# ---------------------------------------------------------------- 会话：循环

def test_run_steps_and_publishes_then_stops_cleanly(fake_rig) -> None:
    """整条循环：步进 → 入历史 → 按墙钟格子投递 → 停线程 → 释放仿真器。

    `sim.close()` 必须在**仿真线程的 finally** 里调（GL 上下文绑线程），
    这里断言它确实被调了，且 `stop()` 之后线程不再存活。
    """
    got: list[SimEvent] = []
    sess, sim = fake_rig(SimConfig(posture="stand"),
                         on_event=got.append, publish_hz=50.0, frame_hz=0.0)
    sess.start()
    assert sess.running is True
    assert _sleep_until(lambda: len(got) > 0, timeout=3.0), "没有任何投递"
    # 再等一小会儿，让 steps 累积得比 published 多（物理 100Hz > 投递 50Hz）
    time.sleep(0.1)
    sess.stop()

    assert sess.running is False
    assert sess.stats.steps > 0
    assert sess.stats.published > 0
    assert len(sess.history(5)) == 5
    assert sim.closed >= 1, "仿真线程必须自己释放渲染器"

    # 投递抽样：物理步数应当多于**投递点数**（50Hz 投递 vs 100Hz 物理）。
    # `published` 计的是信封条数，每点 5 条，故先除再比。
    assert sess.stats.published % 5 == 0, sess.stats
    assert sess.stats.steps > sess.stats.published / 5, sess.stats
    print("PASS test_run_steps_and_publishes_then_stops_cleanly")


def test_start_is_idempotent(fake_rig) -> None:
    sess, _ = fake_rig(SimConfig(posture="stand"), frame_hz=0.0)
    sess.start()
    thread = sess._thread
    sess.start()
    assert sess._thread is thread
    sess.stop(timeout=3.0)
    print("PASS test_start_is_idempotent")


def test_paused_session_does_not_step(fake_rig) -> None:
    """暂停期间不推进物理，但墙钟基准要跟着前移——否则恢复瞬间补跑一大段。"""
    sess, sim = fake_rig(SimConfig(posture="stand"), frame_hz=0.0)
    sess.start()
    assert _sleep_until(lambda: sess.stats.steps > 0, timeout=3.0)
    sess.set_paused(True)
    time.sleep(0.05)
    frozen = sess.stats.steps
    time.sleep(0.15)
    assert sess.stats.steps == frozen, (frozen, sess.stats.steps)
    sess.set_paused(False)
    assert _sleep_until(lambda: sess.stats.steps > frozen, timeout=3.0)
    sess.stop()
    # 恢复后不应一口气补上暂停的 0.2s（200 点）；给到 150 点仍算"没有补跑"
    assert sess.stats.steps - frozen < 150, sess.stats.steps - frozen
    print("PASS test_paused_session_does_not_step")


def test_reconfigure_resets_history_sequence_and_clock(fake_rig) -> None:
    """重建环境 = 重开一局：历史、序列号、墙钟基准全部归零。

    不归零会让 `actual_speed` 拿新一局的 t 除以进程启动以来的总墙钟，
    显示成 0.02x（实际 4.6x）——演示里就是"倍率坏了"。
    """
    sess, sim = fake_rig(SimConfig(posture="stand"))
    sess._hist.extend(_telemetry(i * 0.01) for i in range(10))
    sess._seq = 42
    t0_before = sess._t0

    sess.reconfigure(posture="run", severity=0.5, thermal_scale=2.0)

    assert len(sim.resets) == 1
    new_cfg = sim.resets[0]
    assert new_cfg.posture == "run"
    assert new_cfg.severity == 0.5
    assert new_cfg.thermal_scale == 2.0
    # 未指定的项沿用旧配置，不能被清成默认值
    assert new_cfg.fault == sim.config.fault
    assert sess.history(10) == []
    assert sess._seq == 0
    assert sess._t0 >= t0_before
    assert sess._reset_clock.is_set(), "循环要看到这个标记才会把仿真时钟归零"
    print("PASS test_reconfigure_resets_history_sequence_and_clock")


def test_reconfigure_while_running_rebases_sim_clock(fake_rig) -> None:
    """运行中 reset 后，仿真时钟必须从 0 重新算，而不是接着旧的走。"""
    sess, sim = fake_rig(SimConfig(posture="stand"), frame_hz=0.0)
    sess.start()
    assert _sleep_until(lambda: sim.t > 0.2, timeout=3.0)
    sess.reconfigure(posture="prone")
    assert _sleep_until(lambda: not sess._reset_clock.is_set(), timeout=2.0)
    time.sleep(0.05)
    sess.stop()
    # reset 后 sim.t 从 0 起跳，故最终仍应远小于 reset 前的进程总时长对应值
    assert sim.t < 3.0, sim.t
    print("PASS test_reconfigure_while_running_rebases_sim_clock")


def test_backlog_beyond_max_steps_is_dropped_not_deferred(fake_rig) -> None:
    """追不上就**放弃欠账**，不越欠越多。

    每 tick 上限 `_MAX_STEPS_PER_TICK`（=0.5 仿真秒）。把倍率开到 100x
    且步进很慢时，一个 tick 想补的量远超上限——此时必须把多出来的
    记成 dropped 并前移 `sim_done`，否则仿真时钟会永远落后墙钟
    （观感是"越来越慢、永远追不上"）。
    """
    sess, sim = fake_rig(SimConfig(posture="stand"), frame_hz=0.0)
    sess.speed = 100.0
    sim.step_sleep = 0.03          # 一步进就花掉 6 个 tick 的墙钟
    sess.start()
    assert _sleep_until(lambda: sess.stats.dropped > 0, timeout=3.0), sess.stats
    sess.stop()
    assert sess.stats.dropped >= 50, sess.stats
    # 每 tick 步进都不超过上限（超出的被丢了，不是塞进这一批）
    assert sess.stats.steps <= 50 * 200, sess.stats
    assert sess.stats.steps > 0
    print("PASS test_backlog_beyond_max_steps_is_dropped_not_deferred")


def test_session_stats_defaults() -> None:
    st = SessionStats()
    assert (st.steps, st.published, st.dropped, st.started_at,
            st.wall_elapsed, st.drop_reason) == (0, 0, 0, 0.0, 0.0, None)
    print("PASS test_session_stats_defaults")


# ---------------------------------------------------------------- MQTT

def test_mqtt_disabled_is_an_empty_implementation() -> None:
    """`enabled=False` 时不建客户端、不发任何东西，但**必须说明原因**。

    演示不能因为 broker 没起就停；也不能让页面显示"已连接"。
    """
    pub = MqttTelemetryPublisher(enabled=False)
    assert pub._client is None
    assert pub.stats.last_error == "已关闭（--no-mqtt 或配置）"
    assert pub.publish({"event_id": "x"}) is False
    assert pub.stats.published == 0 and pub.stats.failed == 0
    st = pub.state()
    assert st["enabled"] is False and st["connected"] is False
    assert st["last_error"] == "已关闭（--no-mqtt 或配置）"
    pub.close()                 # 无客户端：必须是 no-op
    print("PASS test_mqtt_disabled_is_an_empty_implementation")


def test_mqtt_topic_uses_tenant_and_device() -> None:
    """topic 约定必须与订阅端一致：`telemetry/<tenant>/<device>`。

    改了这里，仿真数据就再也接不进 `MqttSource`（默认订阅 `telemetry/#`），
    而且不会报错——只是永远收不到。
    """
    pub = MqttTelemetryPublisher(enabled=False, tenant_id="t-1",
                                 device_sn="dev-9", qos=0)
    assert pub.topic == "telemetry/t-1/dev-9"
    assert pub.state()["qos"] == 0
    print("PASS test_mqtt_topic_uses_tenant_and_device")


class _PubInfo:
    def __init__(self, rc: int) -> None:
        self.rc = rc


class _StubClient:
    def __init__(self, *, rc: int = 0, raises: Exception | None = None) -> None:
        self.rc = rc
        self.raises = raises
        self.sent: list[tuple[str, str, int]] = []
        self.loop_stopped = 0
        self.disconnected = 0

    def publish(self, topic: str, payload: str, qos: int = 0):
        if self.raises is not None:
            raise self.raises
        self.sent.append((topic, payload, qos))
        return _PubInfo(self.rc)

    def loop_stop(self) -> None:
        self.loop_stopped += 1

    def disconnect(self) -> None:
        self.disconnected += 1


def test_mqtt_publish_success_counts_and_serializes_utf8() -> None:
    """成功入队 → published+1；载荷必须是 UTF-8 JSON（中文不转义）。

    转成 `\\uXXXX` 也行，但下游日志里就没法直接读了——`ensure_ascii=False`
    是有意的。
    """
    pub = MqttTelemetryPublisher(enabled=False, qos=1)
    client = _StubClient(rc=0)
    pub._client = client
    ok = pub.publish({"component_hint": "电池", "n": 1})
    assert ok is True
    assert pub.stats.published == 1 and pub.stats.failed == 0
    topic, payload, qos = client.sent[0]
    assert topic == pub.topic and qos == 1
    assert "电池" in payload
    json.loads(payload)
    print("PASS test_mqtt_publish_success_counts_and_serializes_utf8")


def test_mqtt_publish_nonzero_rc_is_a_failure_with_reason() -> None:
    pub = MqttTelemetryPublisher(enabled=False)
    pub._client = _StubClient(rc=7)
    assert pub.publish({"a": 1}) is False
    assert pub.stats.failed == 1 and pub.stats.published == 0
    assert pub.stats.last_error == "publish rc=7"
    print("PASS test_mqtt_publish_nonzero_rc_is_a_failure_with_reason")


def test_mqtt_publish_exception_is_swallowed_and_recorded() -> None:
    """broker 挂了不能让仿真线程抛——异常记进 failed/last_error 就够了。"""
    pub = MqttTelemetryPublisher(enabled=False)
    pub._client = _StubClient(raises=ConnectionResetError("reset by peer"))
    assert pub.publish({"a": 1}) is False
    assert pub.stats.failed == 1
    assert "ConnectionResetError: reset by peer" in pub.stats.last_error
    print("PASS test_mqtt_publish_exception_is_swallowed_and_recorded")


def test_mqtt_close_stops_loop_and_disconnects() -> None:
    pub = MqttTelemetryPublisher(enabled=False)
    client = _StubClient()
    pub._client = client
    pub.close()
    assert client.loop_stopped == 1 and client.disconnected == 1
    assert pub._client is None
    pub.close()                 # 第二次是 no-op，不抛
    print("PASS test_mqtt_close_stops_loop_and_disconnects")


def test_mqtt_close_swallows_client_errors() -> None:
    class _Bad(_StubClient):
        def loop_stop(self) -> None:
            raise OSError("socket already closed")

    pub = MqttTelemetryPublisher(enabled=False)
    pub._client = _Bad()
    pub.close()                 # 不许抛
    assert pub._client is None
    print("PASS test_mqtt_close_swallows_client_errors")


class _RC:
    """`paho` 的 ReasonCode 替身（2.x 的 `is_failure` / `value` / `str`）。"""

    def __init__(self, value: int, text: str) -> None:
        self.value = value
        self.is_failure = value != 0
        self._text = text

    def __str__(self) -> str:
        return self._text


def test_mqtt_connect_callback_sets_connected() -> None:
    pub = MqttTelemetryPublisher(enabled=False)
    pub._on_connect(None, None, None, _RC(0, "Success"))
    assert pub.stats.connected is True
    assert pub.stats.last_error is None

    pub._on_connect(None, None, None, _RC(5, "Not authorized"))
    assert pub.stats.connected is False
    assert pub.stats.last_error == "连接被拒：Not authorized"
    print("PASS test_mqtt_connect_callback_sets_connected")


def test_mqtt_disconnect_callback_clears_connected() -> None:
    pub = MqttTelemetryPublisher(enabled=False)
    pub.stats.last_error = None          # 清掉 enabled=False 留的说明，只测回调
    pub.stats.connected = True
    pub._on_disconnect(None, None, None, _RC(0, "Success"))
    assert pub.stats.connected is False
    assert pub.stats.last_error is None      # 正常断开不留错误

    pub.stats.connected = True
    pub._on_disconnect(None, None, None, _RC(7, "Keepalive timeout"))
    assert pub.stats.connected is False
    assert pub.stats.last_error == "断开：Keepalive timeout"

    # reason_code 缺省（老 API 的 2 参回调）也不能炸
    pub._on_disconnect(None, None)
    assert pub.stats.connected is False
    print("PASS test_mqtt_disconnect_callback_clears_connected")


def test_mqtt_missing_paho_reports_install_hint(monkeypatch) -> None:
    """没有 paho 时给出**可执行的**安装提示，而不是沉默。

    `paho` 存在但不是包（没有 `__path__`）时同样走 ModuleNotFoundError
    分支——这模拟的正是"装了别的同名模块"或"装到一半"。
    """
    for name in ("paho", "paho.mqtt", "paho.mqtt.client"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "paho", types.ModuleType("paho"))

    pub = MqttTelemetryPublisher(enabled=True)
    assert pub._client is None
    assert pub.stats.last_error is not None
    assert "paho-mqtt" in pub.stats.last_error, pub.stats.last_error
    assert "data-dog[mqtt]" in pub.stats.last_error, pub.stats.last_error
    print("PASS test_mqtt_missing_paho_reports_install_hint")


def test_mqtt_client_construction_failure_is_recorded(monkeypatch) -> None:
    """paho 内部的构造异常也要降级成 `last_error`，不能冒到服务启动上去。"""
    fake = types.ModuleType("paho.mqtt.client")
    fake.CallbackAPIVersion = types.SimpleNamespace(VERSION2="v2")

    def _boom(*_a, **_kw):
        raise ValueError("bad client id")

    fake.Client = _boom
    monkeypatch.setitem(sys.modules, "paho", types.ModuleType("paho"))
    monkeypatch.setitem(sys.modules, "paho.mqtt", types.ModuleType("paho.mqtt"))
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", fake)

    pub = MqttTelemetryPublisher(enabled=True)
    assert pub._client is None
    assert pub.stats.last_error == "ValueError: bad client id", pub.stats.last_error
    print("PASS test_mqtt_client_construction_failure_is_recorded")


def test_mqtt_client_is_constructed_with_async_connect(monkeypatch) -> None:
    """构造成功的路径：`connect_async` + `loop_start`（不阻塞启动）。

    用 `connect()` 会在 broker 不可达时卡住服务启动——演示的第一步就失败。
    """
    calls: dict[str, Any] = {}

    class _Client:
        def __init__(self, api, client_id: str = "") -> None:
            calls["api"] = api
            calls["client_id"] = client_id
            self.on_connect = None
            self.on_disconnect = None

        def connect_async(self, host, port, keepalive) -> None:
            calls["connect_async"] = (host, port, keepalive)

        def loop_start(self) -> None:
            calls["loop_start"] = True

        def loop_stop(self) -> None:
            calls["loop_stop"] = True

        def disconnect(self) -> None:
            calls["disconnect"] = True

    fake = types.ModuleType("paho.mqtt.client")
    fake.CallbackAPIVersion = types.SimpleNamespace(VERSION2="v2")
    fake.Client = _Client
    monkeypatch.setitem(sys.modules, "paho", types.ModuleType("paho"))
    monkeypatch.setitem(sys.modules, "paho.mqtt", types.ModuleType("paho.mqtt"))
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", fake)

    pub = MqttTelemetryPublisher(enabled=True, host="10.0.0.5", port=1884,
                                 keepalive=45, device_sn="dev-1",
                                 client_id="cid-1")
    assert pub._client is not None
    assert pub.stats.last_error is None
    assert calls["api"] == "v2"
    assert calls["client_id"] == "cid-1"
    assert calls["connect_async"] == ("10.0.0.5", 1884, 45)
    assert calls["loop_start"] is True
    # 默认 client_id 由设备号派生（便于在 broker 上认人）
    pub2 = MqttTelemetryPublisher(enabled=True, device_sn="dev-2")
    assert pub2._client is not None
    assert calls["client_id"] == "go2sim-dev-2"
    pub.close()
    assert calls["loop_stop"] is True and calls["disconnect"] is True
    print("PASS test_mqtt_client_is_constructed_with_async_connect")


# ---------------------------------------------------------------- 控制台：纯函数

def test_telemetry_rows_empty_history() -> None:
    """空历史给**空结构**而不是 None：页面据此画占位，不用特性判断。"""
    assert _telemetry_rows([]) == {"t": 0.0, "joints": [], "series": {}}
    print("PASS test_telemetry_rows_empty_history")


def test_telemetry_rows_current_values_and_120_point_tail() -> None:
    """当前值取最后一个采样；曲线只留最近 120 点。

    12 关节 × 600 点 = 7200 个数字，每 200ms 传一次会明显吃带宽；
    120 点（12 秒 @10Hz）足够看出趋势。
    """
    hist = [_telemetry(i * 0.01, temp=20.0 + i * 0.001) for i in range(200)]
    rows = _telemetry_rows(hist)

    assert rows["t"] == pytest.approx(1.99, abs=1e-6)
    assert len(rows["joints"]) == 12
    assert rows["joints"][0]["name"] == hist[-1].joints[0]
    assert rows["joints"][0]["temp"] == pytest.approx(20.199, abs=0.05)
    assert rows["joints"][0]["current"] == pytest.approx(2.0)
    assert rows["joints"][0]["tau"] == pytest.approx(3.0)
    assert rows["joints"][0]["valid"] is True

    series = rows["series"]
    assert len(series["t"]) == 120
    assert len(series["temp"]) == 120 and len(series["current"]) == 120
    assert series["t"][0] == pytest.approx(0.80, abs=1e-6)
    assert series["t"][-1] == pytest.approx(1.99, abs=1e-6)
    # temp/current 是 12 关节的**逐点最大值**（页面画一条包络线）
    assert series["temp"][-1] == pytest.approx(20.199, abs=0.05)
    assert series["current"][-1] == pytest.approx(2.0)
    print("PASS test_telemetry_rows_current_values_and_120_point_tail")


def test_telemetry_rows_uses_nanmax_over_joints() -> None:
    """某关节丢包（NaN）不该把整条曲线变成 NaN——取逐点最大值。"""
    a = _telemetry(0.1)
    a.temperature = np.full(12, np.nan)
    a.temperature[3] = 88.0
    rows = _telemetry_rows([a])
    assert rows["series"]["temp"] == [88.0], rows["series"]["temp"]
    print("PASS test_telemetry_rows_uses_nanmax_over_joints")


# ---------------------------------------------------------------- 控制台：路由

def test_planes_use_window_size_60_not_default_30() -> None:
    """两条数据面都必须按 60 样本建窗口，退回 `DataPlane` 默认的 30 会让规则恒假。

    `MOTOR_STALL_SUSTAINED` 的前置是 `window.span() >= 5.0`。演示链路按 10Hz
    投递，30 样本只有 **3 秒**——窗口永远够不到 5 秒，这条规则**恒假**。
    实测（F1 堵转 / run / 90 仿真秒）：窗口 30 时正确规则 0 命中，F1 只能靠
    `MOTOR_LOAD_SPIKE` 歪打正着报出来，归因还错到 F 子系统；窗口 60 时
    `MOTOR_STALL_SUSTAINED` 命中 770 帧。

    这是"能跑但永远不报"的那类缺陷——所有接口都 200、所有曲线都正常，
    只有正确的那条规则不出手。所以钉在构造参数上，不靠任何端到端行为间接推。
    """
    from data_dog.go2_sim.server import WINDOW_SIZE

    assert WINDOW_SIZE == 60, "60 样本 @10Hz = 6s，刚好越过 5s 门槛"
    assert WINDOW_SIZE / 10.0 >= 5.0, "窗口时长必须覆盖 span>=5.0 的前置"

    sink = _PlaneSink()
    assert sink.error is None, sink.error
    for label, plane, _comps in sink._planes:
        assert plane.window.size == WINDOW_SIZE, (
            f"{label} 用了 {plane.window.size} 样本窗口，规则会恒假")
    print("PASS test_planes_use_window_size_60_not_default_30")


def test_route_by_component_hint() -> None:
    """分流键就是 `component_hint`，写错会让故障永远检不出。"""
    sink = _PlaneSink()
    assert sink.error is None, sink.error
    motor, subsys = [p for _l, p, _c in sink._planes]
    assert sink._route({"component_hint": "joint_motor.front_left_knee"}) is motor
    assert sink._route({"component_hint": "battery.main"}) is subsys
    assert sink._route({"component_hint": "imu.body"}) is subsys
    assert sink._route({}) is None
    assert sink._route({"component_hint": "joint_motor.front_left_hip"}) is None
    print("PASS test_route_by_component_hint")


def test_unrouted_component_falls_back_to_motor_plane() -> None:
    """没有归属的部件送进电机那条，让它按原语义被拒——**不能静默吞掉**。

    吞掉会让"为什么没有候选"变成谜：明明发了数据，两边都看不见。
    """
    from data_dog.go2_sim.emit import envelope
    sink = _PlaneSink()
    env = envelope(_telemetry(0.5), component="joint_motor.front_left_hip",
                   sequence=1)
    assert env is not None
    sink.push(SimEvent(t=0.5, env=env))
    stats = sink.stats()["planes"]
    assert stats["motor"]["ingested"] == 1, stats
    assert stats["subsystem"]["ingested"] == 0, stats
    # 该部件没挂算子 → 解析/检测都不产生候选
    assert sink.candidates(5) == []
    print("PASS test_unrouted_component_falls_back_to_motor_plane")


def test_push_is_noop_when_no_plane_could_be_built(monkeypatch) -> None:
    """两条数据面都建不起来时 `push` 必须安静返回（页面照跑、如实显示 error）。"""
    from data_dog.data_plane import p2_demo
    from data_dog.go2_sim import subsystem_operator

    def _boom(*_a, **_kw):
        raise RuntimeError("算子配置坏了")

    monkeypatch.setattr(p2_demo, "_detector_config", _boom)
    monkeypatch.setattr(subsystem_operator, "build_subsystem_operator", _boom)

    sink = _PlaneSink()
    assert sink._planes == []
    assert sink.error is not None and "算子配置坏了" in sink.error
    st = sink.stats()
    assert st["available"] is False and st["error"] == sink.error
    from data_dog.go2_sim.emit import envelope
    env = envelope(_telemetry(0.0), sequence=1)
    assert env is not None
    sink.push(SimEvent(t=0.0, env=env))     # 不许抛
    assert sink.candidates(5) == []
    print("PASS test_push_is_noop_when_no_plane_could_be_built")


def test_one_plane_failing_does_not_kill_the_other(monkeypatch) -> None:
    """单条数据面建不起来时，另一条照跑（早期版本是一条坏就整体不产候选）。"""
    from data_dog.data_plane import p2_demo

    def _boom(*_a, **_kw):
        raise RuntimeError("电机算子坏了")

    monkeypatch.setattr(p2_demo, "_detector_config", _boom)
    sink = _PlaneSink()
    labels = [lbl for lbl, _p, _c in sink._planes]
    assert labels == ["subsystem"], labels
    assert sink.error is not None and "motor" in sink.error
    assert sink.stats()["available"] is True
    print("PASS test_one_plane_failing_does_not_kill_the_other")


# ---------------------------------------------------------------- 控制台：HTTP

class _FakeSession:
    """`SimSession` 的替身：端点只用到这些成员。

    真会话一构造就要跑物理（`Go2Sim` 需要 MuJoCo），而端点契约与物理无关
    ——换成替身才能进 CI。
    """

    def __init__(self, config=None, *, publish_hz: float = 10.0,
                 frame_hz: float = 5.0) -> None:
        self.config = config
        self.publish_hz = publish_hz
        self.frame_hz = frame_hz
        self.speed = 1.0
        self.paused = False
        self.started = 0
        self.stopped = 0
        self.injected: list[tuple[str, dict]] = []
        self.reconfigured: list[dict] = []
        self._on_event = None
        self._hist: list[Telemetry] = []
        self._frame = b""
        self._frame_error: str | None = None
        self.sim = types.SimpleNamespace(snapshot=lambda: {"t": 3.5})
        self._speed_calls: list[float] = []

    def start(self) -> None:
        self.started += 1

    def stop(self, timeout: float = 5.0) -> None:
        self.stopped += 1

    def set_speed(self, v: float) -> None:
        self._speed_calls.append(v)
        self.speed = min(100.0, max(0.1, float(v)))

    def set_paused(self, v: bool) -> None:
        self.paused = bool(v)

    def inject(self, key: str, **kw: Any) -> None:
        self.injected.append((key, kw))

    def reconfigure(self, **changes: Any) -> None:
        self.reconfigured.append(changes)

    def history(self, n: int = 200) -> list[Telemetry]:
        return self._hist[-max(1, n):]

    def frame_jpeg(self, width: int | None = None,
                   height: int | None = None) -> bytes:
        return self._frame

    def frame_state(self) -> dict:
        return {"t": 0.0, "bytes": len(self._frame), "size": [880, 550],
                "error": self._frame_error, "hz": self.frame_hz}

    def snapshot(self) -> dict:
        return {"t": 3.5, "speed": self.speed, "paused": self.paused,
                "running": True, "actual_speed": 1.0,
                "publish_hz": self.publish_hz,
                "stats": {"steps": 1, "published": 1, "dropped": 0,
                          "drop_reason": None, "wall_elapsed": 3.5}}


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server_mod, "SimSession", _FakeSession)
    app = server_mod.create_app(config=SimConfig(posture="stand"),
                                publish_hz=20.0, frame_hz=2.0)
    with TestClient(app) as c:
        yield c, app


def test_state_endpoint_reports_speeds_postures_and_plane(client) -> None:
    c, app = client
    assert app.state.session.started == 1, "startup 必须把会话拉起来"
    body = c.get("/api/state").json()
    assert body["speeds"] == list(SPEEDS)
    assert {p["key"] for p in body["postures"]} == {"stand", "run", "prone"}
    assert body["publish_hz"] == 20.0
    assert body["mqtt"] == {"enabled": False}, body["mqtt"]
    assert body["data_plane"]["available"] is True
    assert set(body["data_plane"]["planes"]) == {"motor", "subsystem"}
    assert body["frame"]["hz"] == 2.0
    assert body["server_time"].endswith("+00:00")
    print("PASS test_state_endpoint_reports_speeds_postures_and_plane")


def test_telemetry_endpoint_shape_and_bounds(client) -> None:
    c, app = client
    app.state.session._hist = [_telemetry(i * 0.1) for i in range(50)]
    body = c.get("/api/telemetry", params={"n": 10}).json()
    assert len(body["joints"]) == 12
    assert len(body["series"]["t"]) == 10
    # 越界参数由 FastAPI 挡下（1..600），不是静默截断
    assert c.get("/api/telemetry", params={"n": 0}).status_code == 422
    assert c.get("/api/telemetry", params={"n": 10_000}).status_code == 422
    print("PASS test_telemetry_endpoint_shape_and_bounds")


def test_frame_endpoint_serves_jpeg_and_503_when_not_ready(client) -> None:
    """没有帧时给 503 并说明原因，页面据此显示占位而不是把它当错误。"""
    c, app = client
    r = c.get("/api/frame.jpg")
    assert r.status_code == 503
    assert "画面尚未就绪" in r.json()["detail"]

    app.state.session._frame = _jpg()
    r = c.get("/api/frame.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-store"
    assert r.content[:2] == b"\xff\xd8"
    print("PASS test_frame_endpoint_serves_jpeg_and_503_when_not_ready")


def test_frame_endpoint_reports_render_error_in_detail(client) -> None:
    c, app = client
    app.state.session._frame_error = "RuntimeError: no GL"
    r = c.get("/api/frame.jpg")
    assert r.status_code == 503
    assert "no GL" in r.json()["detail"], r.json()
    print("PASS test_frame_endpoint_reports_render_error_in_detail")


def test_faults_endpoint_lists_library_and_postures(client) -> None:
    c, _app = client
    body = c.get("/api/faults").json()
    assert len(body["faults"]) == 24, len(body["faults"])
    assert {"key", "label"} <= set(body["faults"][0])
    assert {p["key"] for p in body["postures"]} == {"stand", "run", "prone"}
    print("PASS test_faults_endpoint_lists_library_and_postures")


def test_candidates_endpoint_returns_list_and_stats(client) -> None:
    c, app = client
    body = c.get("/api/candidates", params={"limit": 5}).json()
    assert body["candidates"] == []
    assert set(body["plane"]["planes"]) == {"motor", "subsystem"}

    # 灌一条越界数据 → 有候选，且 limit 生效
    from data_dog.go2_sim.emit import subsystem_envelopes
    from tests.test_go2_sim_subsystems import FakeSysTelemetry

    sink = app.state.sink
    for env in subsystem_envelopes(FakeSysTelemetry(battery={"soc": 5.0}),
                                   sequence=1):
        sink.push(SimEvent(t=1.0, env=env))
    cands = c.get("/api/candidates", params={"limit": 1}).json()["candidates"]
    assert len(cands) == 1 and cands[0]["rule_matches"] == ["POWER_SOC_LOW"]
    print("PASS test_candidates_endpoint_returns_list_and_stats")


def test_control_endpoint_applies_speed_and_pause(client) -> None:
    c, app = client
    body = c.post("/api/control", json={"speed": 4.0}).json()
    assert body == {"ok": True, "speed": 4.0, "paused": False}, body
    body = c.post("/api/control", json={"paused": True}).json()
    assert body["paused"] is True and body["speed"] == 4.0
    assert app.state.session._speed_calls == [4.0]
    # 只给 paused 时不该动倍率
    assert c.post("/api/control", json={}).json()["speed"] == 4.0
    print("PASS test_control_endpoint_applies_speed_and_pause")


def test_inject_endpoint_rejects_unknown_key(client) -> None:
    """未知故障要**当场** 400，不能静默接受。

    静默接受的结果是：页面显示"已注入"，数据流里什么也没变，
    演示时无法解释为什么曲线不动。
    """
    c, app = client
    r = c.post("/api/inject", json={"key": "NOPE"})
    assert r.status_code == 400
    assert "未知故障" in r.json()["detail"]
    assert app.state.session.injected == []

    r = c.post("/api/inject", json={"key": "F1_stall", "severity": 0.8,
                                    "duration_s": 10.0, "start_s": 0.0})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["fault"] == "F1_stall"
    assert body["at_sim_t"] == 3.5
    assert app.state.session.injected == [
        ("F1_stall", {"severity": 0.8, "duration_s": 10.0, "start_s": 0.0})]
    print("PASS test_inject_endpoint_rejects_unknown_key")


def test_reset_endpoint_rejects_unknown_posture_and_clears_candidates(
        client) -> None:
    """reset 必须同时重建仿真**和**清空候选/数据面。

    只重建仿真的话，reset 到 normal 之后页面还挂着上一局的 8 条候选，
    观众会以为正常工况就在报故障。`None` 字段不能传下去（否则会把
    severity/seed 覆盖成 None）。
    """
    c, app = client
    r = c.post("/api/reset", json={"posture": "fly"})
    assert r.status_code == 400
    assert "未知姿态" in r.json()["detail"]
    assert app.state.session.reconfigured == []

    sink = app.state.sink
    from data_dog.go2_sim.emit import subsystem_envelopes
    from tests.test_go2_sim_subsystems import FakeSysTelemetry

    for env in subsystem_envelopes(FakeSysTelemetry(battery={"soc": 5.0}),
                                   sequence=1):
        sink.push(SimEvent(t=1.0, env=env))
    assert sink.candidates(5), "前置条件：应该有候选"

    r = c.post("/api/reset", json={"posture": "run", "severity": 0.5})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert app.state.session.reconfigured == [
        {"posture": "run", "severity": 0.5}], app.state.session.reconfigured
    assert sink.candidates(5) == []
    print("PASS test_reset_endpoint_rejects_unknown_posture_and_clears_candidates")


def test_index_serves_console_html(client) -> None:
    c, _app = client
    r = c.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<html" in r.text.lower()
    print("PASS test_index_serves_console_html")


def test_shutdown_stops_session_and_closes_mqtt(monkeypatch) -> None:
    """退出时必须停会话并关 MQTT，否则 paho 的网络线程会拖着进程。

    `mqtt_host` 给了才建 publisher；这里用替身，避免真连 broker。
    """
    from fastapi.testclient import TestClient

    closed: list[str] = []

    class _FakePub:
        def __init__(self, **_kw) -> None:
            self.published: list[dict] = []

        def publish(self, env: dict) -> bool:
            self.published.append(env)
            return True

        def state(self) -> dict:
            return {"enabled": True}

        def close(self) -> None:
            closed.append("mqtt")

    monkeypatch.setattr(server_mod, "SimSession", _FakeSession)
    monkeypatch.setattr(server_mod, "MqttTelemetryPublisher", _FakePub)
    app = server_mod.create_app(mqtt_host="127.0.0.1")
    with TestClient(app) as c:
        assert c.get("/api/state").json()["mqtt"] == {"enabled": True}
        # 两个出口同时投递，不是二选一
        app.state.session._on_event(SimEvent(t=0.0, env={"a": 1}))
        assert app.state.mqtt.published == [{"a": 1}]
    assert closed == ["mqtt"], closed
    assert app.state.session.stopped == 1
    print("PASS test_shutdown_stops_session_and_closes_mqtt")


# ---------------------------------------------------------------- 控制台：CLI

def test_main_builds_app_from_argv(monkeypatch) -> None:
    """CLI 参数 → SimConfig → uvicorn.run。默认值也是契约的一部分。"""
    monkeypatch.setattr(server_mod, "SimSession", _FakeSession)
    seen: dict[str, Any] = {}

    def _run(app, host: str, port: int, log_level: str) -> None:
        seen["app"] = app
        seen["host"] = host
        seen["port"] = port
        seen["log_level"] = log_level

    monkeypatch.setitem(sys.modules, "uvicorn",
                        types.SimpleNamespace(run=_run))

    rc = server_mod.main(["--port", "9999", "--posture", "run",
                          "--fault", "F1_stall", "--speed", "4.0",
                          "--thermal-scale", "2.5", "--publish-hz", "20",
                          "--frame-hz", "0"])
    assert rc == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 9999
    assert seen["log_level"] == "info"

    sess = seen["app"].state.session
    assert sess.config.posture == "run"
    assert sess.config.fault == "F1_stall"
    assert sess.config.thermal_scale == 2.5
    assert sess.publish_hz == 20.0 and sess.frame_hz == 0.0
    assert sess.speed == 4.0, "CLI 的 --speed 要落到会话上"
    print("PASS test_main_builds_app_from_argv")


def test_main_defaults(monkeypatch) -> None:
    monkeypatch.setattr(server_mod, "SimSession", _FakeSession)
    seen: dict[str, Any] = {}
    monkeypatch.setitem(sys.modules, "uvicorn",
                        types.SimpleNamespace(run=lambda app, **kw: seen.update(kw)))
    assert server_mod.main([]) == 0
    assert seen["port"] == 8099 and seen["host"] == "127.0.0.1"
    print("PASS test_main_defaults")
