"""P4-3 升级引擎 / 值班表 / 端点测试。

覆盖 docs/p4/03_notify_oncall.md §5 的验收：
  2. 升级：ack_deadline 到点未 ack → sweeper 升到 level+1，写新 alert_delivery 行。
  3. 停止：ack 后不再升级。
  4. 幂等：同 alert 同 level 只投一次（delivery_id 确定性）。
  5. 升级链尽头：最高 level 无 ack → failed，不无限升级。
  6. 租户隔离 + 缺头 401。
外加：值班表解析（域匹配/级别择低/时间窗）、无值班人失败关闭、端点级。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from data_dog.alerting.escalation import (
    EscalationEngine,
    make_delivery_id,
)
from data_dog.alerting.lifecycle import AlertEvent
from data_dog.alerting.oncall import (
    RotaEntry,
    current_oncall,
    make_rota_id,
    resolve_oncall,
)

_Q = {"X-Tenant-Id": "tenant-a"}
_T2 = {"X-Tenant-Id": "tenant-b"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'esc.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    monkeypatch.setenv("DD_INVESTIGATE_OFFLINE", "1")
    # 默认**关**通知——测试里逐条显式开；SSRF 放行以便内网假 server 通道测试用
    monkeypatch.setenv("DD_NOTIFY_ALLOW_PRIVATE", "1")
    monkeypatch.setenv("DD_NOTIFY_MAX_LEVEL", "3")
    from data_dog.storage.db import init_db, reset_engine
    reset_engine()
    init_db(url)
    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        yield c


def _seed_rota(tenant: str, *, domain: str, level: int, assignee: str,
               channel: str = "webhook", target: str = "http://127.0.0.1:1/x",
               start: float | None = None, end: float | None = None) -> str:
    """种一条值班条目。返回 rota_id。"""
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    now = time.time()
    start = now - 60 if start is None else start
    end = now + 3600 if end is None else end
    rid = make_rota_id(domain, level, start)
    with session_scope(tenant_id=tenant) as s:
        repo.upsert_rota(s, tenant_id=tenant, rota_id=rid, domain=domain,
                         level=level, assignee=assignee, start_at=start,
                         end_at=end, channel=channel, target=target)
    return rid


def _seed_alert(tenant: str, event_id: str = "alert_x",
                component: str = "joint_motor.front_hip") -> str:
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    ev = AlertEvent(event_id=event_id, idempotency_key=f"k-{event_id}",
                    device_ref="device-abc", component_ref=component,
                    event_type="overheat", state="confirmed",
                    severity="critical", summary="过热")
    with session_scope(tenant_id=tenant) as s:
        repo.save_alert(s, ev, tenant_id=tenant)
    return event_id


def _expire_deadlines(tenant: str, *, delta: float = 5.0) -> None:
    """把该租户所有 pending 投递的 ack_deadline 拨到过去（模拟 SLA 超时）。"""
    from data_dog.storage import models as M
    from data_dog.storage.db import session_scope
    with session_scope(tenant_id=tenant) as s:
        for r in s.query(M.AlertDeliveryRow).filter(
                M.AlertDeliveryRow.tenant_id == tenant,
                M.AlertDeliveryRow.status == "pending").all():
            r.ack_deadline = time.time() - delta


def _deliveries(tenant: str) -> list[dict]:
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    with session_scope(tenant_id=tenant) as s:
        return repo.list_deliveries(s, tenant_id=tenant)


# ---------------------------------------------------------------- 值班表解析

def test_resolve_oncall_lowest_level_wins():
    now = 1000.0
    entries = [
        RotaEntry("r1", "joint_motor", 2, "bob", now - 10, now + 10),
        RotaEntry("r2", "joint_motor", 1, "alice", now - 10, now + 10),
    ]
    e = resolve_oncall(entries, domain="joint_motor", at=now)
    assert e is not None and e.assignee == "alice"      # 一线优先


def test_resolve_oncall_wildcard_domain():
    now = 1000.0
    entries = [RotaEntry("r1", "*", 1, "global", now - 10, now + 10)]
    assert resolve_oncall(entries, domain="bms", at=now).assignee == "global"


def test_resolve_oncall_out_of_window_none():
    now = 1000.0
    entries = [RotaEntry("r1", "joint_motor", 1, "a", now + 10, now + 20)]
    assert resolve_oncall(entries, domain="joint_motor", at=now) is None


def test_current_oncall_falls_back_higher_level():
    """一线空 → 回退二线（只升不降）。"""
    now = 1000.0
    entries = [RotaEntry("r2", "joint_motor", 2, "bob", now - 10, now + 10)]
    assert current_oncall(entries, domain="joint_motor", at=now).assignee == "bob"


# ---------------------------------------------------------------- 2. 升级

def test_escalation_upgrades_on_sla_breach(client):
    _seed_rota("tenant-a", domain="joint_motor", level=1, assignee="alice")
    _seed_rota("tenant-a", domain="joint_motor", level=2, assignee="bob")
    _seed_alert("tenant-a")

    eng = EscalationEngine()
    res = eng.dispatch(tenant_id="tenant-a",
                       alert=_alert(client))
    assert res.level == 1 and res.assignee == "alice"

    _expire_deadlines("tenant-a")
    stats = eng.sweep_once()
    assert stats["escalated"] == 1
    rows = {(r["level"], r["status"]) for r in _deliveries("tenant-a")}
    assert (1, "escalated") in rows and (2, "pending") in rows


def _alert(client, event_id: str = "alert_x") -> dict:
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    with session_scope(tenant_id="tenant-a") as s:
        return repo.get_alert(s, event_id, tenant_id="tenant-a")


# ---------------------------------------------------------------- 3. 停止

def test_ack_stops_escalation(client):
    _seed_rota("tenant-a", domain="joint_motor", level=1, assignee="alice")
    _seed_rota("tenant-a", domain="joint_motor", level=2, assignee="bob")
    _seed_alert("tenant-a")
    eng = EscalationEngine()
    eng.dispatch(tenant_id="tenant-a", alert=_alert(client))

    acked = eng.ack(tenant_id="tenant-a", event_id="alert_x")
    assert acked == 1
    # 已 ack → 再过 deadline 也不升级
    _expire_deadlines("tenant-a")
    assert eng.sweep_once()["escalated"] == 0
    assert {r["status"] for r in _deliveries("tenant-a")} == {"acked"}


def test_ack_advances_alert_to_recovering(client):
    _seed_rota("tenant-a", domain="joint_motor", level=1, assignee="alice")
    _seed_alert("tenant-a")
    EscalationEngine().dispatch(tenant_id="tenant-a", alert=_alert(client))
    EscalationEngine().ack(tenant_id="tenant-a", event_id="alert_x")
    assert _alert(client)["state"] == "recovering"


# ---------------------------------------------------------------- 4. 幂等

def test_delivery_id_deterministic():
    assert make_delivery_id("alert_x", 1) == make_delivery_id("alert_x", 1)
    assert make_delivery_id("alert_x", 1) != make_delivery_id("alert_x", 2)


def test_same_level_not_re_delivered(client):
    """同一告警同一级重复 dispatch → 只有一行（attempts 递增，不新增）。"""
    _seed_rota("tenant-a", domain="joint_motor", level=1, assignee="alice")
    _seed_alert("tenant-a")
    eng = EscalationEngine()
    eng.dispatch(tenant_id="tenant-a", alert=_alert(client))
    eng.dispatch(tenant_id="tenant-a", alert=_alert(client))
    rows = _deliveries("tenant-a")
    assert len(rows) == 1 and rows[0]["attempts"] == 2


# ---------------------------------------------------------------- 5. 升级链尽头

def test_chain_end_marks_failed(client):
    for lv in (1, 2, 3):
        _seed_rota("tenant-a", domain="joint_motor", level=lv,
                   assignee=f"a{lv}")
    _seed_alert("tenant-a")
    eng = EscalationEngine()
    eng.dispatch(tenant_id="tenant-a", alert=_alert(client))
    # 反复超时升级：1→2→3→failed（不无限升）
    for _ in range(4):
        _expire_deadlines("tenant-a")
        eng.sweep_once()
    rows = {(r["level"], r["status"]) for r in _deliveries("tenant-a")}
    assert (3, "failed") in rows
    assert all(r["level"] <= 3 for r in _deliveries("tenant-a"))


# ---------------------------------------------------------------- 无值班人

def test_no_oncall_fails_closed(client):
    _seed_alert("tenant-a")                 # 无值班表
    res = EscalationEngine().dispatch(tenant_id="tenant-a", alert=_alert(client))
    assert not res.ok and res.reason == "no_oncall"
    assert _deliveries("tenant-a") == []    # 不投递、不留台账


# ---------------------------------------------------------------- 端点级

def test_put_and_get_oncall(client):
    now = time.time()
    r = client.put("/v1/oncall", headers=_Q, json={
        "domain": "joint_motor", "level": 1, "assignee": "alice",
        "start_at": now - 10, "end_at": now + 3600,
        "channel": "webhook", "target": "http://127.0.0.1:1/x"})
    assert r.status_code == 200, r.text
    r = client.get("/v1/oncall", headers=_Q)
    body = r.json()
    assert body["count"] == 1
    assert body["resolved"]["assignee"] == "alice"


def test_put_oncall_validates_window(client):
    now = time.time()
    r = client.put("/v1/oncall", headers=_Q, json={
        "domain": "x", "level": 1, "assignee": "a",
        "start_at": now, "end_at": now - 5})
    assert r.status_code == 422


def test_ack_endpoint(client):
    _seed_rota("tenant-a", domain="joint_motor", level=1, assignee="alice")
    _seed_alert("tenant-a")
    EscalationEngine().dispatch(tenant_id="tenant-a", alert=_alert(client))
    r = client.post("/v1/alerts/alert_x/ack", headers=_Q)
    assert r.status_code == 200 and r.json()["acked"] == 1
    r2 = client.post("/v1/alerts/alert_x/ack", headers=_Q)   # 幂等
    assert r2.json()["acked"] == 0


def test_ack_unknown_alert_404(client):
    assert client.post("/v1/alerts/nope/ack", headers=_Q).status_code == 404


def test_notifications_endpoint(client):
    _seed_rota("tenant-a", domain="joint_motor", level=1, assignee="alice")
    _seed_alert("tenant-a")
    EscalationEngine().dispatch(tenant_id="tenant-a", alert=_alert(client))
    r = client.get("/v1/notifications", headers=_Q)
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_endpoints_require_tenant(client):
    assert client.get("/v1/oncall").status_code == 401
    assert client.get("/v1/notifications").status_code == 401
    assert client.post("/v1/alerts/x/ack").status_code == 401


def test_tenant_isolation(client):
    """tenant-b 看不到 tenant-a 的投递记录（租户作用域）。"""
    _seed_rota("tenant-a", domain="joint_motor", level=1, assignee="alice")
    _seed_alert("tenant-a")
    EscalationEngine().dispatch(tenant_id="tenant-a", alert=_alert(client))
    assert client.get("/v1/notifications", headers=_T2).json()["count"] == 0
    assert client.get("/v1/oncall", headers=_T2).json()["count"] == 0
