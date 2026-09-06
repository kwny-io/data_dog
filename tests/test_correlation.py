# -*- coding: utf-8 -*-
"""A2: 多事件确定性关联的雏形测试。"""
from data_dog.correlation import Correlator, signal_family
from data_dog.schemas.events import CandidateEvent


def _cand(cid: str, device: str, component: str, signal: str) -> CandidateEvent:
    return CandidateEvent(
        candidate_id=cid, device_sn=device, component_ref=component,
        signal=signal, severity_hint="warning",
        window_start="09:55", window_end="10:00", sample_count=100,
    )


def test_correlate_groups_same_family():
    corr = Correlator()
    cands = [
        _cand("c1", "dev1", "joint_motor.front_left_hip", "temperature_rising_under_normalized_load"),
        _cand("c2", "dev1", "joint_motor.front_left_knee", "temperature_rising_under_normalized_load"),
        _cand("c3", "dev1", "battery.main", "voltage_drop"),  # 不同信号族
    ]
    groups = corr.correlate(cands)
    # 两个 thermal 归为一组，electrical 自成一类 → 期望 2 组
    assert len(groups) == 2
    thermal = [g for g in groups if g.family == "thermal"][0]
    assert thermal.candidate_ids == ["c1", "c2"]
    assert thermal.component_refs == ["joint_motor.front_left_hip", "joint_motor.front_left_knee"]
    print("PASS test_correlate_groups_same_family")


def test_signal_family_mapping():
    assert signal_family("temperature_spike") == "thermal"
    assert signal_family("voltage_drop") == "electrical"
    assert signal_family("something_new") == "unknown"
    print("PASS test_signal_family_mapping")


def test_topology_power_domain_link():
    """A2 拓扑：同设备+同窗+同电源域的不同信号族组应相互标记为拓扑关联。"""
    from data_dog.correlation import Correlator
    corr = Correlator()
    cands = [
        # 电池电压下降（electrical）— power_domain=power_bus
        _cand("b1", "dev1", "battery.main", "voltage_drop"),
        # 左髋电机温度爬升（thermal）— power_domain=power_bus，同窗
        _cand("m1", "dev1", "joint_motor.front_left_hip", "temperature_rising_under_normalized_load"),
        # 另一个设备（不同设备不关联）
        _cand("m2", "dev2", "joint_motor.front_left_hip", "temperature_rising_under_normalized_load"),
    ]
    groups = corr.correlate(cands)
    # 三种信号族 → 3 组
    assert len(groups) == 3
    by_fam = {g.family: g for g in groups}
    # dev1 的 battery + motor 同属 power_bus 且同窗 → 互相关联
    assert by_fam["electrical"].power_domain == "power_bus"
    assert by_fam["thermal"].power_domain == "power_bus"
    # 直接验证 dev1 的 battery 和 dev1 的 thermal 互相在 linked
    battery = by_fam["electrical"]
    thermal_dev1 = [g for g in groups if g.family == "thermal" and g.device_sn == "dev1"][0]
    assert thermal_dev1.group_id in battery.linked_group_ids
    assert battery.group_id in thermal_dev1.linked_group_ids
    print("PASS test_topology_power_domain_link")
