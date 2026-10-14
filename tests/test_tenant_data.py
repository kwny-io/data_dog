"""租户级数据导出与删除（⑥）：合规里的"数据可携"与"被遗忘权"。

要守住的六件事
-------------
1. **导出不越租户**：只回自己租户的行（这是"数据可携"的底线，越了就是数据泄露）。
2. **有界且不静默截断**：每表有上限，超限必须显式回 `truncated`——有界快照不能
   被当成全量导出。
3. **删除门槛更高**：只有运维面能调，且必须在 `X-Confirm-Tenant` 里**原样复述**
   租户 id；不一致就**一行都不删**（删除不可逆，没有回滚）。
4. **只删目标租户**：另一个租户的数据必须完好（删错租户是合规事故里最严重的一类）。
5. **删除本身要留痕，且留在系统名下**：如果把"某租户已被删除"记在该租户名下，
   它会随该租户的 `audit_log` 一起被删掉 —— 等于**没有证据证明删除发生过**。
6. **表清单动态发现**：新增带 `tenant_id` 的表必须自动进入导出/删除范围
   （写死清单的话，漏掉一张表就是"用户要求删除但你漏了"）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from data_dog import audit, settings, tenant_data
from data_dog.storage.db import init_db, reset_engine
from data_dog.storage.models import Base

OPS = "ops-token-xyz"


@pytest.fixture()
def url(tmp_path, monkeypatch):
    u = f"sqlite:///{(tmp_path / 'tenant_data.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", u)
    reset_engine()
    init_db(u)
    yield u
    reset_engine()


@pytest.fixture()
def client(url, monkeypatch):
    from fastapi.testclient import TestClient

    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        yield c


def _seed_telemetry(u: str, tenant: str, n: int, prefix: str = "e") -> None:
    eng = create_engine(u)
    with eng.begin() as c:
        c.execute(Base.metadata.tables["telemetry_events"].insert(), [
            {"tenant_id": tenant, "event_id": f"{tenant}-{prefix}{i}",
             "device_sn": "SN1", "occurred_at": "2026-01-01T00:00:00Z",
             "component_ref": "leg.hip", "created_at": 1.0,
             "created_day": 0} for i in range(n)])   # 日桶 = floor(1.0/86400)（A9）
    eng.dispose()


def _count(u: str, table: str, tenant: str) -> int:
    eng = create_engine(u)
    with eng.connect() as c:
        n = c.execute(text(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = :t"),
                      {"t": tenant}).scalar()
    eng.dispose()
    return int(n or 0)


def _hdr(tenant=None, ops=None, confirm=None):
    h = {}
    if tenant:
        h["X-Tenant-Id"] = tenant
    if ops:
        h["X-Ops-Token"] = ops
    if confirm:
        h["X-Confirm-Tenant"] = confirm
    return h


# ---------------------------------------------------------------- 导出

def test_export_returns_only_own_tenant_rows(url):
    """导出**不越租户**：t1 的快照里不能出现 t2 的任何数据。"""
    _seed_telemetry(url, "t1", 2)
    _seed_telemetry(url, "t2", 3)

    snap = tenant_data.export_tenant("t1")

    assert snap["exported_rows"] == 2
    assert snap["row_counts"]["telemetry_events"] == 2
    ids = [r["event_id"] for r in snap["data"]["telemetry_events"]]
    assert all(i.startswith("t1-") for i in ids), f"导出了别的租户的数据：{ids}"


def test_export_marks_truncation_instead_of_silently_cutting(url):
    """有界快照必须**显式**标出被截断，不能让人误以为拿到了全量。"""
    _seed_telemetry(url, "t1", 5)

    snap = tenant_data.export_tenant("t1", per_table_limit=2)

    assert snap["exported_rows"] == 2
    assert snap["truncated"] is True
    assert "telemetry_events" in snap["truncated_tables"]
    assert snap["row_counts"]["telemetry_events"] == 5, "应给出真实总数以供判断"
    assert len(snap["data"]["telemetry_events"]) == 2


def test_export_full_when_under_limit(url):
    _seed_telemetry(url, "t1", 2)
    snap = tenant_data.export_tenant("t1", per_table_limit=10)
    assert snap["truncated"] is False and snap["truncated_tables"] == []


def test_export_writes_audit_under_the_tenant(url):
    """导出是"把自己全部数据拷走"，属敏感动作 → 留痕（记在该租户名下）。"""
    _seed_telemetry(url, "t1", 1)
    tenant_data.export_tenant("t1")
    rows = audit.query(tenant_id="t1", action=audit.ACTION_TENANT_EXPORT)["items"]
    assert rows, "导出没有留痕"


def test_export_endpoint_needs_tenant_header(client):
    assert client.get("/v1/tenant/export").status_code == 401


def test_export_endpoint_returns_snapshot(client, url):
    _seed_telemetry(url, "t1", 1)
    r = client.get("/v1/tenant/export", headers=_hdr(tenant="t1"))
    assert r.status_code == 200
    assert r.json()["tenant_id"] == "t1"


def test_export_limit_is_configurable(url, monkeypatch):
    monkeypatch.setenv("DD_TENANT_EXPORT_LIMIT", "3")
    assert settings.tenant_export_limit() == 3
    monkeypatch.setenv("DD_TENANT_EXPORT_LIMIT", "0")
    assert settings.tenant_export_limit() == 1, "0/负数要收敛到 1，而不是「无上限」"


# ---------------------------------------------------------------- 删除：两道闸

def test_purge_endpoint_requires_ops_token(client, monkeypatch):
    monkeypatch.setenv("DD_OPS_TOKEN", OPS)
    r = client.post("/v1/tenant/purge", headers=_hdr(confirm="t1"),
                    json={"tenant_id": "t1"})
    assert r.status_code == 401          # 没有 X-Ops-Token


def test_purge_endpoint_hidden_when_ops_disabled(client, monkeypatch):
    monkeypatch.delenv("DD_OPS_TOKEN", raising=False)
    r = client.post("/v1/tenant/purge", headers=_hdr(ops=OPS, confirm="t1"),
                    json={"tenant_id": "t1"})
    assert r.status_code == 404          # 未启用运维面 → 端点不暴露


def test_purge_requires_matching_confirmation_and_deletes_nothing(client, url, monkeypatch):
    """确认串不一致 → 400，且**一行都不删**（删除不可逆，宁可拒绝）。"""
    monkeypatch.setenv("DD_OPS_TOKEN", OPS)
    _seed_telemetry(url, "t1", 3)

    r = client.post("/v1/tenant/purge", headers=_hdr(ops=OPS, confirm="typo-tenant"),
                    json={"tenant_id": "t1"})

    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "confirm_required"
    assert _count(url, "telemetry_events", "t1") == 3, "确认失败却删了东西"


def test_purge_missing_confirmation_also_refused(client, url, monkeypatch):
    monkeypatch.setenv("DD_OPS_TOKEN", OPS)
    _seed_telemetry(url, "t1", 1)
    r = client.post("/v1/tenant/purge", headers=_hdr(ops=OPS),
                    json={"tenant_id": "t1"})
    assert r.status_code == 400
    assert _count(url, "telemetry_events", "t1") == 1


# ---------------------------------------------------------------- 删除：语义

def test_purge_deletes_only_target_tenant(client, url, monkeypatch):
    """删 t1，t2 必须完好——删错租户是最严重的合规事故。"""
    monkeypatch.setenv("DD_OPS_TOKEN", OPS)
    _seed_telemetry(url, "t1", 3)
    _seed_telemetry(url, "t2", 4)

    r = client.post("/v1/tenant/purge", headers=_hdr(ops=OPS, confirm="t1"),
                    json={"tenant_id": "t1"})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["total"] == 3
    assert _count(url, "telemetry_events", "t1") == 0
    assert _count(url, "telemetry_events", "t2") == 4, "把别的租户也删了"


def test_purge_dry_run_deletes_nothing(url):
    _seed_telemetry(url, "t1", 3)
    report = tenant_data.purge_tenant("t1", dry_run=True)
    assert report["total"] == 3
    assert _count(url, "telemetry_events", "t1") == 3


def test_purge_is_bounded_and_reports_capped(url):
    _seed_telemetry(url, "t1", 5)
    report = tenant_data.purge_tenant("t1", batch_size=2, max_batches=2)
    row = next(r for r in report["tables"] if r["table"] == "telemetry_events")
    assert (row["deleted"], row["capped"]) == (4, True)
    assert _count(url, "telemetry_events", "t1") == 1


def test_purge_record_lives_under_system_tenant(url):
    """**关键**：删除证据必须留在系统名下。

    若记在该租户名下，它会随该租户的 `audit_log` 一起被删掉 —— 那就**没有证据
    证明删除发生过**，而这正是合规复盘时唯一要看的记录。
    """
    _seed_telemetry(url, "t1", 2)
    tenant_data.purge_tenant("t1")

    sys_rows = audit.query(tenant_id=audit.SYSTEM_TENANT,
                           action=audit.ACTION_TENANT_PURGE)["items"]
    assert sys_rows, "删除没有留痕（或留在了会被自己删掉的地方）"
    rec = sys_rows[0]
    assert rec["detail"]["purged_tenant"] == "t1"
    assert rec["detail"]["total_deleted"] == 2
    # 该租户自己的审计已经被清空（除了……没有例外：证据在系统名下）
    assert audit.query(tenant_id="t1", action=audit.ACTION_TENANT_PURGE)["items"] == []


def test_purge_refuses_system_tenant(url):
    """保留租户 `-` 存放无租户上下文的系统/运维事件，不能被"某租户删除"顺手清掉。"""
    with pytest.raises(ValueError):
        tenant_data.purge_tenant(audit.SYSTEM_TENANT)


def test_purge_rejects_empty_tenant(url):
    with pytest.raises(ValueError):
        tenant_data.purge_tenant("   ")


def test_purge_endpoint_rejects_system_tenant_with_400(client, monkeypatch):
    monkeypatch.setenv("DD_OPS_TOKEN", OPS)
    r = client.post("/v1/tenant/purge", headers=_hdr(ops=OPS, confirm="-"),
                    json={"tenant_id": "-"})
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "invalid_tenant"


# ---------------------------------------------------------------- 表清单

def test_tenant_tables_is_derived_from_metadata_not_hardcoded():
    """表清单必须**动态发现**：带 `tenant_id` 的表一个都不能漏。

    漏一张表 = "用户要求删除，但你漏了" —— 这是合规事故，不是小瑕疵。这条断言把
    "清单写死然后与模型漂移"这件事变成红灯。
    """
    have = {name for name, t in Base.metadata.tables.items() if "tenant_id" in t.c}
    assert set(tenant_data.tenant_tables()) == have
    assert len(have) >= 5, "元数据里带 tenant_id 的表少得可疑"


def test_audit_log_is_purged_last():
    """审计表排最后：先清租户自己的审计，再写"这次删除"的系统侧记录。"""
    assert tenant_data.tenant_tables()[-1] == "audit_log"
