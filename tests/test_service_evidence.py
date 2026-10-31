"""`RepoEvidenceBridge` 测试：从落库遥测重建 Agent 证据源。

守住两件关键事：
  1. 键名映射复用 `scenarios._STANDARD_NAME`（temperature → temperature_c），
     否则 Agent 证据会与 demo/前端静默分叉。
  2. 白名单/设备越权 → denied（与其它证据源同形）。
"""
from __future__ import annotations

from data_dog.service.evidence import RepoEvidenceBridge


def _rows():
    return [
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:00:00Z",
         "component_ref": "joint_motor.front_left_hip",
         "metrics": {"temperature": 58, "current": 6.0}},
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:05:00Z",
         "component_ref": "joint_motor.front_left_hip",
         "metrics": {"temperature": 96, "current": 12.0}},
    ]


def test_metrics_keys_are_standardized():
    b = RepoEvidenceBridge(_rows(), device_sn="dev-1")
    res = b.query_telemetry("dev-1", "joint_motor.front_left_hip",
                            "2026-08-13T10:05:00Z", "2026-08-13T10:05:00Z")
    assert res is not None
    # 标准名映射生效（与 Agent 证据侧一致）
    assert res["metrics"] == {"temperature_c": 96.0, "current_a": 12.0}


def test_reference_window_is_previous_row():
    """参照窗 = 同部件更早一行，Agent 据此看出窗口内抬升。"""
    b = RepoEvidenceBridge(_rows(), device_sn="dev-1")
    res = b.query_telemetry("dev-1", "joint_motor.front_left_hip",
                            "2026-08-13T10:05:00Z", "2026-08-13T10:05:00Z")
    assert res["reference_window_metrics"] == {"temperature_c": 58.0,
                                               "current_a": 6.0}


def test_sustained_fault_uses_peak_not_last_sample():
    """持续故障的窗口末行是"解除瞬间"——代表值必须取窗口内异常样本。

    2026-10-31 实跑 F1 堵转时正是这条路径把真故障判成了误报：5 分钟窗口里
    10956 行是 14.98A，最后 3 行是恢复后的 0.29A，`rows[-1]` 取到后者，
    Agent 据此写下"候选声称电流≥14.5A，但实测 0.295A → 误报"，告警因此不产生。
    """
    rows = [
        # 故障前：正常电流
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:00:00Z",
         "component_ref": "joint_motor.front_left_hip",
         "metrics": {"current": 0.8, "temperature": 25}},
        # 窗口内：持续堵转
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:01:00Z",
         "component_ref": "joint_motor.front_left_hip",
         "metrics": {"current": 14.97, "temperature": 38}},
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:02:00Z",
         "component_ref": "joint_motor.front_left_hip",
         "metrics": {"current": 14.98, "temperature": 38}},
        # 窗口末：故障解除
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:03:00Z",
         "component_ref": "joint_motor.front_left_hip",
         "metrics": {"current": 0.29, "temperature": 38}},
    ]
    b = RepoEvidenceBridge(rows, device_sn="dev-1")
    res = b.query_telemetry("dev-1", "joint_motor.front_left_hip",
                            "2026-08-13T10:01:00Z", "2026-08-13T10:03:00Z")
    assert res["metrics"]["current_a"] == 14.98, (
        f"代表值取到了 {res['metrics']['current_a']}A；末行是解除瞬间的 0.29A，"
        "取它会把这个真堵转判成误报"
    )
    # 参照必须是窗口**之前**的正常值，不能是窗口内部的相邻故障样本
    assert res["reference_window_metrics"]["current_a"] == 0.8
    # 窗口统计让 Agent 能判断"过半时间在阈值之上"
    assert res["window_metrics"]["current_a"]["max"] == 14.98
    assert res["window_metrics"]["current_a"]["min"] == 0.29
    assert res["sample_count"] == 3


def test_timestamps_compare_by_time_not_string():
    """整秒时间戳不带小数位，字典序会把它排到同秒其它样本之后。

    `...:03Z` vs `...:03.970000Z`：`.`(0x2E) < `Z`(0x5A)，字符串比较认为前者
    更大。仿真器投递的样本里 2545 行是这种整秒形态，末行取样因此屡屡取错。
    """
    rows = [
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:01:00Z",
         "component_ref": "joint_motor.front_left_hip", "metrics": {"current": 1.0}},
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:01:00.970000Z",
         "component_ref": "joint_motor.front_left_hip", "metrics": {"current": 9.0}},
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:01:02Z",
         "component_ref": "joint_motor.front_left_hip", "metrics": {"current": 2.0}},
    ]
    b = RepoEvidenceBridge(rows, device_sn="dev-1")
    # 窗口只含前两条：按真实时间，10:01:00.97 是窗口内最后一条
    res = b.query_telemetry("dev-1", "joint_motor.front_left_hip",
                            "2026-08-13T10:01:00Z", "2026-08-13T10:01:01Z")
    assert res["sample_count"] == 2, (
        f"窗口内样本数 {res['sample_count']}，第三条（10:01:02）不该被纳入"
    )
    # 参照窗是窗口之前的一条——这里窗口从第一条开始，故无参照
    assert res["reference_window_metrics"] is None


def test_unknown_component_denied():
    b = RepoEvidenceBridge(_rows(), device_sn="dev-1")
    res = b.query_telemetry("dev-1", "not_in_allowlist", "a", "b")
    assert isinstance(res, dict) and res.get("ok") is False


def test_device_mismatch_denied():
    b = RepoEvidenceBridge(_rows(), device_sn="dev-1")
    res = b.query_telemetry("other-dev", "joint_motor.front_left_hip", "a", "b")
    assert isinstance(res, dict) and res.get("ok") is False


def test_missing_component_returns_none():
    b = RepoEvidenceBridge(_rows(), device_sn="dev-1")
    # 在白名单内但库里没有该部件 → None（工具层转 not_found）
    assert b.query_telemetry("dev-1", "battery.main", "a", "b") is None


def test_neighbor_events_denied_for_unknown_component():
    b = RepoEvidenceBridge(_rows(), device_sn="dev-1")
    res = b.query_neighbor_events("dev-1", "not_in_allowlist", "a", "b")
    assert isinstance(res, dict) and res.get("ok") is False


def test_neighbor_events_finds_anomalous_sibling():
    """真关联：同设备**其它部件**在同窗内命中真实算子规则 → 作为邻居事件返回。

    用 `battery.main` 的 BATT_SOC_LOW（soc <= 0.2，出自真实算子 YAML）构造。
    """
    rows = [
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:00:00Z",
         "component_ref": "joint_motor.front_left_hip",
         "metrics": {"temperature": 96, "current": 12.0}},
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:00:00Z",
         "component_ref": "battery.main", "metrics": {"soc": 0.1}},
    ]
    b = RepoEvidenceBridge(rows, device_sn="dev-1")
    evs = b.query_neighbor_events("dev-1", "joint_motor.front_left_hip",
                                  "2026-08-13T10:00:00Z", "2026-08-13T10:00:00Z")
    assert isinstance(evs, list) and len(evs) == 1
    ev = evs[0]
    assert ev["component"] == "battery.main"
    assert ev["rule_id"] == "BATT_SOC_LOW"
    assert ev["event"] == "BATT_SOC_LOW"


def test_neighbor_excludes_self_and_healthy_siblings():
    """自身不算邻居；正常部件（未越阈）不产生邻居事件。"""
    rows = [
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:00:00Z",
         "component_ref": "joint_motor.front_left_hip",
         "metrics": {"temperature": 96, "current": 12.0}},
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:00:00Z",
         "component_ref": "battery.main", "metrics": {"soc": 0.9}},  # 正常
    ]
    b = RepoEvidenceBridge(rows, device_sn="dev-1")
    evs = b.query_neighbor_events("dev-1", "joint_motor.front_left_hip",
                                  "2026-08-13T10:00:00Z", "2026-08-13T10:00:00Z")
    assert evs == []


def test_neighbor_ignores_sibling_outside_window():
    """邻居异常若发生在窗口外 → 不算同窗邻居。"""
    rows = [
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:00:00Z",
         "component_ref": "joint_motor.front_left_hip",
         "metrics": {"temperature": 96, "current": 12.0}},
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T09:00:00Z",
         "component_ref": "battery.main", "metrics": {"soc": 0.1}},
    ]
    b = RepoEvidenceBridge(rows, device_sn="dev-1")
    evs = b.query_neighbor_events("dev-1", "joint_motor.front_left_hip",
                                  "2026-08-13T10:00:00Z", "2026-08-13T10:00:00Z")
    assert evs == []


def test_recent_changes_available():
    b = RepoEvidenceBridge(_rows(), device_sn="dev-1")
    changes = b.query_recent_changes("dev-1", "joint_motor.front_left_hip", "2026-08-10")
    assert "changes" in changes


# ---------------------------------------------------------------- G6 因果链

def test_causal_chain_parent_to_child():
    """电池先异常（t=0）、电机后异常（t=2s）、拓扑同链（电池是电机祖先）
    → 产出 parent_to_child 因果链，lag=2.0。"""
    rows = [
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:00:00Z",
         "component_ref": "battery.main", "metrics": {"soc": 0.1}},   # BATT_SOC_LOW
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:00:02Z",
         "component_ref": "joint_motor.front_left_hip",              # 温度异常
         "metrics": {"temperature": 96, "current": 12.0}},
    ]
    b = RepoEvidenceBridge(rows, device_sn="dev-1")
    res = b.query_causal_chain("dev-1", "joint_motor.front_left_hip",
                               "2026-08-13T10:00:00Z", "2026-08-13T10:00:05Z")
    chains = res["causal_chains"]
    assert chains, "应产出因果链"
    lk = chains[0]
    assert lk["cause_ref"] == "battery.main"
    assert lk["effect_ref"] == "joint_motor.front_left_hip"
    assert lk["lag_s"] == 2.0
    assert lk["direction"] == "parent_to_child"
    assert lk["suspect"] is False


def test_causal_chain_denied_for_unknown_component():
    b = RepoEvidenceBridge(_rows(), device_sn="dev-1")
    res = b.query_causal_chain("dev-1", "not_in_allowlist", "a", "b")
    assert isinstance(res, dict) and res.get("ok") is False


def test_causal_chain_non_monotonic_events_no_link():
    """同期异常（无先后）→ 无因果链。"""
    rows = [
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:00:00Z",
         "component_ref": "battery.main", "metrics": {"soc": 0.1}},
        {"device_sn": "dev-1", "occurred_at": "2026-08-13T10:00:00Z",
         "component_ref": "joint_motor.front_left_hip",
         "metrics": {"temperature": 96}},
    ]
    b = RepoEvidenceBridge(rows, device_sn="dev-1")
    res = b.query_causal_chain("dev-1", "joint_motor.front_left_hip",
                               "2026-08-13T10:00:00Z", "2026-08-13T10:00:05Z")
    assert res["causal_chains"] == []

