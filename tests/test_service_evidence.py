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
