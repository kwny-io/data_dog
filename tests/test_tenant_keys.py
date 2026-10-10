"""租户绑定密钥（`DD_TENANT_KEYS`）：把"持密钥者可冒充任意租户"这个洞钉死。

背景（实测出来的洞）
-------------------
全局单密钥模式下，`DD_API_KEY` 只回答"你是不是某个合法调用方"，租户身份却来自
调用方自报的 `X-Tenant-Id`。实测：**同一把密钥**分别以 `victim-a` / `victim-b`
身份写入遥测，两次都是 200；读回来也各自成立。也就是说一个密钥 = 全租户通吃，
RLS 与复合主键挡不住这种"主动冒充"（它们挡的是代码写错导致的串租户）。

本文件锁定两件事：
1. **向后兼容**：未配 `DD_TENANT_KEYS` 时行为与改动前完全一致（单密钥模式）。
2. **绑定模式真能挡住冒充**：密钥与所声称的租户不一致 → 403，且未知租户与不符
   返回同一状态码（不泄露"哪些租户存在"）。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from data_dog.storage.db import init_db, reset_engine

GLOBAL = "global-admin-key"
KEY_A = "tenant-a-key"
KEY_B = "tenant-b-key"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'tenant_keys.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    reset_engine()
    init_db(url)

    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        yield c
    reset_engine()


def _raw(event_id="e1", temperature=96):
    return {
        "event_id": event_id, "occurred_at": "2026-09-13T10:00:00Z",
        "received_at": "2026-09-13T10:00:00.100Z", "sequence": 1,
        "vendor": "unitree", "model": "go2", "firmware": "3.2.1",
        "protocol_version": "motor-log-v2",
        "component_hint": "joint_motor.front_left_hip",
        "payload_inline": json.dumps({"temperature": temperature, "current": 12.0,
                                      "mode": 0}),
    }


def _hdr(tenant=None, key=None):
    h = {"X-Device-Sn": "go2-sn-001"}
    if tenant is not None:
        h["X-Tenant-Id"] = tenant
    if key is not None:
        h["X-API-Key"] = key
    return h


def _ingest(client, tenant, key, event_id="e1"):
    return client.post("/v1/telemetry", headers=_hdr(tenant, key),
                       json={"events": [_raw(event_id)]})


def _list(client, tenant, key):
    return client.get("/v1/candidates", headers=_hdr(tenant, key))


def _enable_tenant_keys(monkeypatch):
    monkeypatch.setenv("DD_TENANT_KEYS",
                       json.dumps({"tenant-a": KEY_A, "tenant-b": KEY_B}))
    monkeypatch.setenv("DD_API_KEY", GLOBAL)


# ---------------------------------------------------------------- 模式 2：向后兼容

def test_global_key_mode_is_unchanged_when_tenant_keys_unset(client, monkeypatch):
    """未配 DD_TENANT_KEYS → 老行为：全局密钥对所有租户都有效（显式记录，不是遗漏）。"""
    monkeypatch.delenv("DD_TENANT_KEYS", raising=False)
    monkeypatch.setenv("DD_API_KEY", GLOBAL)
    assert _ingest(client, "victim-a", GLOBAL).status_code == 200
    assert _ingest(client, "victim-b", GLOBAL, "e2").status_code == 200


def test_global_key_mode_still_rejects_wrong_key(client, monkeypatch):
    monkeypatch.delenv("DD_TENANT_KEYS", raising=False)
    monkeypatch.setenv("DD_API_KEY", GLOBAL)
    assert _ingest(client, "tenant-a", "nope").status_code == 401
    assert _ingest(client, "tenant-a", None).status_code == 401


# ---------------------------------------------------------------- 模式 1：租户绑定

def test_tenant_key_works_for_its_own_tenant(client, monkeypatch):
    _enable_tenant_keys(monkeypatch)
    assert _ingest(client, "tenant-a", KEY_A).status_code == 200
    assert _list(client, "tenant-a", KEY_A).json()["count"] == 1


def test_same_key_cannot_impersonate_another_tenant(client, monkeypatch):
    """本文件的核心：**这是修复前实测 200 的那个场景**，现在必须 403。"""
    # 与真实探测同形：victim-a / victim-b 各自有绑定密钥
    monkeypatch.setenv("DD_API_KEY", GLOBAL)
    monkeypatch.setenv("DD_TENANT_KEYS",
                       json.dumps({"victim-a": KEY_A, "victim-b": KEY_B}))
    assert _ingest(client, "victim-a", KEY_A).status_code == 200
    # 同一把密钥换成另一个租户头 → 挡住（修复前：200，直接写进别人租户）
    r = _ingest(client, "victim-b", KEY_A, "e2")
    assert r.status_code == 403
    assert r.json()["reason"] == "tenant_key_mismatch"
    assert _list(client, "victim-b", KEY_A).status_code == 403
    # 用自己绑定的密钥仍可正常读写（不是把功能一起关掉）
    assert _list(client, "victim-a", KEY_A).json()["count"] == 1


def test_cross_tenant_read_with_other_tenants_key_is_denied(client, monkeypatch):
    _enable_tenant_keys(monkeypatch)
    assert _ingest(client, "tenant-a", KEY_A).status_code == 200
    # 拿 B 的密钥去读 A → 403（连"是否存在"都不回答）
    assert _list(client, "tenant-a", KEY_B).status_code == 403


def test_unknown_tenant_and_mismatch_are_indistinguishable(client, monkeypatch):
    """不泄露租户是否存在：未知租户与密钥不符返回同一状态码/同一 reason。"""
    _enable_tenant_keys(monkeypatch)
    unknown = _ingest(client, "no-such-tenant", KEY_A)
    mismatch = _ingest(client, "tenant-b", KEY_A, "e2")
    assert unknown.status_code == mismatch.status_code == 403
    assert unknown.json()["reason"] == mismatch.json()["reason"]


def test_global_admin_key_cannot_touch_tenant_scope(client, monkeypatch):
    """绑定模式下全局密钥不再是"万能钥匙"——否则冒充只是换个人来做。"""
    _enable_tenant_keys(monkeypatch)
    assert _ingest(client, "tenant-a", GLOBAL).status_code == 403
    assert _list(client, "tenant-a", GLOBAL).status_code == 403


def test_missing_key_in_tenant_mode_is_denied(client, monkeypatch):
    _enable_tenant_keys(monkeypatch)
    assert _ingest(client, "tenant-a", None).status_code == 403


def test_tenant_isolation_still_holds_between_bound_keys(client, monkeypatch):
    """两个租户各用自己的密钥：数据互不可见（应用层 + 密钥层双保险）。"""
    _enable_tenant_keys(monkeypatch)
    assert _ingest(client, "tenant-a", KEY_A).status_code == 200
    assert _list(client, "tenant-a", KEY_A).json()["count"] == 1
    assert _list(client, "tenant-b", KEY_B).json()["count"] == 0


# ---------------------------------------------------------------- 非租户作用域

def test_probes_stay_public(client, monkeypatch):
    _enable_tenant_keys(monkeypatch)
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200


def test_metrics_needs_global_key_not_tenant_key(client, monkeypatch):
    """`/metrics` 是全局运维口径、不分租户 → 走全局密钥，租户密钥不应放行。"""
    _enable_tenant_keys(monkeypatch)
    assert client.get("/metrics", headers=_hdr(None, GLOBAL)).status_code == 200
    assert client.get("/metrics", headers=_hdr(None, KEY_A)).status_code == 401
    assert client.get("/metrics").status_code == 401


# ---------------------------------------------------------------- 配置错误

def test_malformed_tenant_keys_fails_closed_with_503(client, monkeypatch):
    """配置坏掉绝不退回单密钥模式（那等于漏洞照旧）→ 503 失败关闭。"""
    monkeypatch.setenv("DD_TENANT_KEYS", "{not json")
    monkeypatch.setenv("DD_API_KEY", GLOBAL)
    r = _ingest(client, "tenant-a", KEY_A)
    assert r.status_code == 503
    assert r.json()["reason"] == "tenant_keys_misconfigured"
    # 探针不受影响（编排层不该因此摘流量/重启）
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("bad", ['{"a":""}', '[]', '"str"', '{"": "k"}', "{}"])
def test_invalid_tenant_keys_shapes_are_rejected(client, monkeypatch, bad):
    monkeypatch.setenv("DD_TENANT_KEYS", bad)
    monkeypatch.setenv("DD_API_KEY", GLOBAL)
    assert _ingest(client, "tenant-a", KEY_A).status_code == 503
