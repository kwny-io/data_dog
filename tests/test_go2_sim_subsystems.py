"""非电机子系统这条线：信封 → 路由 → 两条数据面 → 候选。

为什么单独一个文件
------------------
`test_go2_sim_pure.py` 钉的是电机那条线的契约。子系统（电池 / IMU /
散热 / 整机）走的是**同一个网关、同一个 Parser/Detector**，但有两处
结构性差异，且都属于"改起来看着无害、实际会让演示全线失灵"的地方：

1. **一台设备只能有一个绑定**。`CompatibilityResolver.resolve()` 只按
   vendor/model/firmware/protocol 匹配、**不看部件**，多于一个就
   `ambiguous` 失败关闭。所以两个算子包必须各自挂在一个独立的
   `DataPlane` 上，靠信封的 `component_hint` 分流——不是"多注册几个
   绑定"（那样整台设备一条候选都不会有）。
2. **子系统状态必须随 reset 复位**。`BatteryModel` 的 SOC / 包温只写在
   构造函数里；不复位的话服务连跑几分钟后 `/api/reset` 换姿态，正常工况
   一上来就报 POWER_SOC_LOW + BATTERY_OVERHEAT（实测 soc=0）。

这些用例都不需要 MuJoCo：`_PlaneSink` 只依赖算子配置与数据面。

约定同 `test_go2_sim_pure.py`：每个 test 结尾 `print("PASS <name>")`。
"""
from __future__ import annotations

import json

import pytest

from data_dog.go2_sim.emit import (
    DEMO_COMPONENT,
    SUBSYSTEM_SOURCES,
    payload_for_subsystem,
    subsystem_envelopes,
)
from data_dog.go2_sim.server import _PlaneSink
from data_dog.go2_sim.subsystem_operator import build_subsystem_operator


class _State:
    """够用的假子系统状态：只要有 `as_metrics()` 即可。"""

    def __init__(self, metrics: dict[str, float]) -> None:
        self._m = metrics

    def as_metrics(self) -> dict[str, float]:
        return dict(self._m)


class FakeSysTelemetry:
    """带四个子系统的遥测替身。

    字段名照 `emit.payload_for_subsystem` 认的属性名（battery / imu /
    thermal / system），不照部件名——那一层映射在 `SUBSYSTEM_SOURCES` 里。
    """

    def __init__(self, *, t: float = 1.0, **over: dict[str, float]) -> None:
        self.t = t
        base = {
            "battery": {"soc": 100.0, "status": 0, "cycle": 0, "power_v": 25.04,
                        "power_a": 3.0, "current": 3.0, "bq_ntc": 25.0,
                        "mcu_ntc": 26.5, "cell_v_max": 4.18, "cell_v_min": 4.17,
                        "cell_v_spread": 0.005},
            "imu": {"roll": 0.0, "pitch": 0.0, "yaw": 0.0,
                    "gyro_x": 0.0, "gyro_y": 0.0, "gyro_z": 0.0,
                    "accel_x": 0.0, "accel_y": 0.0, "accel_z": 9.81,
                    "accel_norm": 9.81, "imu_temp": 30.0},
            "thermal": {"fan_frequency": 0.30, "temperature_ntc1": 26.0,
                        "bandwidth": 500, "crc_fail_rate": 0.0},
            "system": {"error_code": 0, "gait_type": 0, "body_height": 0.25,
                       "progress": 0.0, "foot_raise_height": 0.08,
                       "pos_x": 0.0, "pos_y": 0.0},
        }
        for comp_attr, fields in over.items():
            base[comp_attr].update(fields)
        for attr, metrics in base.items():
            setattr(self, attr, _State(metrics))


def _sink() -> _PlaneSink:
    sink = _PlaneSink()
    assert sink.error is None, sink.error
    return sink


def _push_all(sink: _PlaneSink, t: FakeSysTelemetry, *, seq: int = 1) -> int:
    from data_dog.go2_sim.session import SimEvent
    envs = subsystem_envelopes(t, sequence=seq)
    for e in envs:
        sink.push(SimEvent(t=float(t.t), env=e))
    return len(envs)


# ---------------------------------------------------------------- 装配

def test_sink_builds_two_independent_planes() -> None:
    """电机与子系统必须是**两条**数据面。

    合并成一条会得到 `ambiguous`——`resolve()` 不看部件，两个绑定
    同时匹配同一台设备。那正是本文件开头写的第 1 条。
    """
    sink = _sink()
    labels = [lbl for lbl, _p, _c in sink._planes]
    assert labels == ["motor", "subsystem"], labels
    print("PASS test_sink_builds_two_independent_planes")


def test_subsystem_plane_covers_all_four_components() -> None:
    """四个部件都要有归属，否则那一路的信封没人认领。"""
    sink = _sink()
    owned = {c for _l, _p, comps in sink._planes for c in comps}
    assert owned == {DEMO_COMPONENT, *SUBSYSTEM_SOURCES}, owned
    print("PASS test_subsystem_plane_covers_all_four_components")


def test_registering_both_operators_would_be_ambiguous() -> None:
    """把两个算子注册进**同一个** resolver 会失败关闭——钉住这个约束。

    这条不是在测现有实现，是防"以后有人图省事把两条合成一条"。
    它必须继续报 ambiguous，否则说明 resolver 的语义被改了。
    """
    from data_dog.data_plane.p2_demo import _detector_config
    from data_dog.data_plane.pipeline import build_demo_resolver

    rv = build_demo_resolver(_detector_config())
    from data_dog.data_plane.pipeline import _compat_from
    from data_dog.data_plane.resolver import OperatorBinding

    other = OperatorBinding(
        operator_id="x", version="1.0.0",
        compat=_compat_from(build_subsystem_operator()),
    )
    rv.register(other)
    ok, out = rv.resolve(vendor="unitree", model="go2", firmware="3.2.1",
                         protocol_version="motor-log-v2")
    assert not ok
    assert out["error"] == "ambiguous", out
    print("PASS test_registering_both_operators_would_be_ambiguous")


# ---------------------------------------------------------------- 载荷

def test_all_subsystem_fields_are_optional() -> None:
    """字段声明必须全 optional。

    一条电机信封里没有 `soc`；声明成必填会给每条信封塞一个
    `__missing__`，`parse_confidence` 被压到 0.8——故障照报，但置信度
    被无谓污染。缺字段在这里是**正常**，不是解析失败。
    """
    cfg = build_subsystem_operator()
    required = [f.name for f in cfg.fields if not f.optional]
    assert required == [], required
    print("PASS test_all_subsystem_fields_are_optional")


def test_battery_payload_uses_power_a_not_current() -> None:
    """电池载荷不能出现 `current`。

    `BmsState.current` 与关节电机电流同名不同义。`Parser.parse` 按
    `source` 取值，两个同名字段会读到同一个数——95A 的关节电流会原样
    写进电池判据。用厂商名 `power_a` 就结构上不可能撞。
    """
    payload = payload_for_subsystem(FakeSysTelemetry(), "battery.main")
    assert payload is not None
    assert "current" not in payload, payload
    assert "power_a" in payload, payload
    print("PASS test_battery_payload_uses_power_a_not_current")


def test_unknown_component_has_no_subsystem_payload() -> None:
    assert payload_for_subsystem(FakeSysTelemetry(), "joint_motor.fl_hip") is None
    print("PASS test_unknown_component_has_no_subsystem_payload")


@pytest.mark.parametrize("component", list(SUBSYSTEM_SOURCES))
def test_each_subsystem_emits_one_envelope(component: str) -> None:
    """每个部件一条信封，且 `component_hint` 必须与部件名一致。

    `component_hint` 是**路由键**：写错就路由到错误的数据面，
    表现是"这条故障永远检不出"。
    """
    envs = subsystem_envelopes(FakeSysTelemetry(), sequence=3)
    assert len(envs) == len(SUBSYSTEM_SOURCES), len(envs)
    one = [e for e in envs if e["component_hint"] == component]
    assert len(one) == 1, [e["component_hint"] for e in envs]
    assert isinstance(one[0]["payload_inline"], str)
    json.loads(one[0]["payload_inline"])
    print(f"PASS test_each_subsystem_emits_one_envelope[{component}]")


def test_non_finite_value_drops_whole_envelope() -> None:
    """任一项非有限 → 整条不发（与电机侧同一语义）。

    补一个假值会把"没有读数"伪装成"读数正常"，下游就再也不会
    因为这一路数据缺失而报警。
    """
    t = FakeSysTelemetry(battery={"soc": float("nan")})
    envs = subsystem_envelopes(t, sequence=1)
    assert all(e["component_hint"] != "battery.main" for e in envs)
    assert len(envs) == len(SUBSYSTEM_SOURCES) - 1
    print("PASS test_non_finite_value_drops_whole_envelope")


# ---------------------------------------------------------------- 分流与检出

def test_subsystem_events_route_to_subsystem_plane() -> None:
    """子系统信封不能落进电机那条数据面。"""
    sink = _sink()
    _push_all(sink, FakeSysTelemetry())
    stats = sink.stats()["planes"]
    assert stats["subsystem"]["resolved"] == len(SUBSYSTEM_SOURCES), stats
    assert stats["motor"]["ingested"] == 0, stats
    print("PASS test_subsystem_events_route_to_subsystem_plane")


@pytest.mark.parametrize(("over", "rule"), [
    ({"battery": {"soc": 8.0}}, "POWER_SOC_LOW"),
    ({"battery": {"cell_v_spread": 0.08}}, "BATTERY_CELL_IMBALANCE"),
    ({"battery": {"bq_ntc": 60.0}}, "BATTERY_OVERHEAT"),
    ({"battery": {"power_a": 35.0}}, "POWER_BUS_OVERCURRENT"),
    ({"battery": {"power_v": 19.0}}, "POWER_BUS_VOLTAGE_LOW"),
    ({"thermal": {"fan_frequency": 0.0}}, "COM_FAN_STALLED"),
    ({"thermal": {"crc_fail_rate": 0.05}}, "COM_CRC_ERROR_RATE"),
    ({"thermal": {"bandwidth": 340}}, "COM_BANDWIDTH_LOW"),
    ({"system": {"body_height": 0.06}}, "POSTURE_FALLEN"),
    ({"system": {"error_code": 4}}, "SYS_ERROR_CODE_SET"),
])
def test_single_fault_produces_its_rule(over: dict, rule: str) -> None:
    """单点越界 → 恰好那一条规则命中，不多不少。

    "不多"很重要：这条的上一版曾经在正常数据上报 IMU/GYRO 零偏
    （奔跑时陀螺摆动 −0.86~+0.81，而阈值是 0.05）。所以这里同时断言
    **只有**目标规则命中——一旦有人加回单帧 IMU 阈值，这个用例会红。
    """
    sink = _sink()
    _push_all(sink, FakeSysTelemetry(**over))
    cands = sink.candidates(20)
    assert len(cands) == 1, [c.get("rule_matches") for c in cands]
    assert cands[0]["rule_matches"] == [rule], cands[0]

    from data_dog.go2_sim.emit import SUBSYSTEM_SOURCES
    assert cands[0]["component_ref"] in SUBSYSTEM_SOURCES, cands[0]
    print(f"PASS test_single_fault_produces_its_rule[{rule}]")


def test_normal_subsystem_data_produces_no_candidates() -> None:
    """正常工况必须一条候选都不出。

    这是整个演示最不能错的一句：观众看到"正常"却在报警，
    后面所有诊断都不可信。IMU 三条（F16/F17/F21）不在单帧算子里的
    原因也正是这个——它们单帧判不了，会稳定误报。
    """
    sink = _sink()
    for seq in range(1, 21):
        _push_all(sink, FakeSysTelemetry(t=float(seq) * 0.1), seq=seq)
    assert sink.candidates(20) == [], sink.candidates(20)
    print("PASS test_normal_subsystem_data_produces_no_candidates")


@pytest.mark.parametrize(("field", "normal_value", "fault_value", "rule"), [
    # 实测边界：正常奔跑负载下 power_v 最低 23.54；F14 掉到 18.69~20.03。
    ("power_v", 23.5, 18.7, "POWER_BUS_VOLTAGE_LOW"),
    # bandwidth 是离散两档，没有中间值。
    ("bandwidth", 1000, 340, "COM_BANDWIDTH_LOW"),
])
def test_measured_threshold_margins(field: str, normal_value: float,
                                    fault_value: float, rule: str) -> None:
    """阈值必须落在实测的正常下界与故障值之间。

    这两条阈值不是拍的，是从数据里量出来的：正常工况最坏值（奔跑负载
    24A）与故障读数之间有明显间隙。若哪天有人放宽到"正常也会碰"的位置，
    这个用例会红——比在演示时才发现误报便宜得多。
    """
    comp = "battery.main" if field == "power_v" else "thermal.main"
    attr = "battery" if field == "power_v" else "thermal"

    sink = _sink()
    _push_all(sink, FakeSysTelemetry(**{attr: {field: normal_value}}))
    assert sink.candidates(10) == [], f"正常值 {normal_value} 不该触发 {rule}"

    sink = _sink()
    _push_all(sink, FakeSysTelemetry(**{attr: {field: fault_value}}))
    cands = sink.candidates(10)
    assert [c["rule_matches"] for c in cands] == [[rule]], cands
    assert cands[0]["component_ref"] == comp, cands[0]
    print(f"PASS test_measured_threshold_margins[{rule}]")


def test_reset_clears_candidates_and_planes() -> None:
    """重开一局必须把上一局的候选与窗口一起清掉。

    不清的话 `/api/reset` 到 normal 之后页面还挂着上一局的故障，
    演示里会被讲成"正常工况也在报故障"。
    """
    sink = _sink()
    _push_all(sink, FakeSysTelemetry(battery={"soc": 5.0}))
    assert sink.candidates(5), "前置条件：应该先有候选"
    sink.reset()
    assert sink.candidates(5) == []
    stats = sink.stats()
    assert stats["normalized"] == 0 and stats["rejected"] == 0, stats
    assert set(stats["planes"]) == {"motor", "subsystem"}, stats["planes"]
    print("PASS test_reset_clears_candidates_and_planes")
