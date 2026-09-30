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


def test_linked_group_roles_have_direction():
    """G6：拓扑关联带方向——电池组对电机组是 upstream，反之 downstream。"""
    from data_dog.correlation import Correlator
    groups = Correlator().correlate([
        _cand("b1", "dev1", "battery.main", "voltage_drop"),
        _cand("m1", "dev1", "joint_motor.front_left_hip", "temperature_spike"),
    ])
    by_fam = {g.family: g for g in groups}
    batt, motor = by_fam["electrical"], by_fam["thermal"]
    # 电池是电机祖先 → 电池（self）相对电机是 upstream，电机相对电池是 downstream
    assert batt.linked_group_roles[motor.group_id] == "upstream"
    assert motor.linked_group_roles[batt.group_id] == "downstream"


def test_window_tolerance_enabled_for_iso_windows():
    """G6：window_tolerance 已启用——ISO 窗口相差在容差内也关联。"""
    from data_dog.correlation import Correlator

    def _iso(cid, comp, signal, ws, we):
        from data_dog.schemas.events import CandidateEvent
        return CandidateEvent(
            candidate_id=cid, device_sn="dev1", component_ref=comp, signal=signal,
            severity_hint="warning", window_start=ws, window_end=we, sample_count=10)

    corr = Correlator(window_tolerance="00:05")   # 5 分钟 = 300s
    # 电池 10:00 窗口、电机 10:03 窗口（差 180s ≤ 300s）→ 拓扑关联
    groups = corr.correlate([
        _iso("b1", "battery.main", "voltage_drop",
             "2026-08-13T10:00:00Z", "2026-08-13T10:01:00Z"),
        _iso("m1", "joint_motor.front_left_hip", "temperature_spike",
             "2026-08-13T10:03:00Z", "2026-08-13T10:04:00Z"),
    ])
    by_fam = {g.family: g for g in groups}
    batt, motor = by_fam["electrical"], by_fam["thermal"]
    assert motor.group_id in batt.linked_group_ids
    assert batt.group_id in motor.linked_group_ids


def test_window_tolerance_excludes_far_windows():
    """超过容差的窗口不关联（容差有界）。"""
    from data_dog.correlation import Correlator
    from data_dog.schemas.events import CandidateEvent

    def _iso(cid, comp, signal, ws, we):
        return CandidateEvent(
            candidate_id=cid, device_sn="dev1", component_ref=comp, signal=signal,
            severity_hint="warning", window_start=ws, window_end=we, sample_count=10)

    corr = Correlator(window_tolerance="00:05")   # 300s
    groups = corr.correlate([
        _iso("b1", "battery.main", "voltage_drop",
             "2026-08-13T10:00:00Z", "2026-08-13T10:01:00Z"),
        _iso("m1", "joint_motor.front_left_hip", "temperature_spike",
             "2026-08-13T11:00:00Z", "2026-08-13T11:01:00Z"),   # 差 1 小时
    ])
    by_fam = {g.family: g for g in groups}
    assert by_fam["electrical"].linked_group_ids == []
    assert by_fam["thermal"].linked_group_ids == []
