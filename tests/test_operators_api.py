"""P4-2 控制面 HTTP 端点测试（FastAPI + TestClient）。

覆盖：未配密钥→503（失败关闭）/ 注册验签 / 状态转移 / 灰度 / 看门狗自动回滚 /
跨租户 404。用临时文件 SQLite 隔离库。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from data_dog.operator_factory.signing import sign_package
from data_dog.storage.db import init_db, reset_engine

SECRET = "api-secret"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'op.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    monkeypatch.setenv("DD_OPERATOR_SIGNING_KEY", SECRET)
    monkeypatch.setenv("DD_CANARY_MIN_SAMPLES", "10")
    reset_engine()
    init_db(url)
    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        yield c
    reset_engine()


_T1 = {"X-Tenant-Id": "t1"}
_T2 = {"X-Tenant-Id": "t2"}


def _body(oid="op.temp", ver="1.0.0", src="temperature >= 75"):
    sig = sign_package(oid, ver, src, secret=SECRET).to_dict()
    return {"operator_id": oid, "version": ver, "dsl_source": src, "signature": sig}


# ---------------------------------------------------------------- 注册（§5.2）

def test_register_requires_tenant(client):
    assert client.post("/v1/operators", json=_body()).status_code == 401


def test_register_ok(client):
    r = client.post("/v1/operators", json=_body(), headers=_T1)
    assert r.status_code == 200
    assert r.json()["operator"]["state"] == "Registered"


def test_register_tampered_rejected(client):
    b = _body()
    b["dsl_source"] = "temperature >= 999"   # 签名与内容不符
    r = client.post("/v1/operators", json=b, headers=_T1)
    assert r.status_code == 422
    assert "验签" in r.json()["detail"]


def test_register_missing_field_422(client):
    r = client.post("/v1/operators", json={"operator_id": "x"}, headers=_T1)
    assert r.status_code == 422


def test_register_no_key_503(client, monkeypatch):
    """未配置签名凭据 → 503，绝不静默放行。"""
    monkeypatch.delenv("DD_OPERATOR_SIGNING_KEY", raising=False)
    r = client.post("/v1/operators", json=_body(), headers=_T1)
    assert r.status_code == 503


# ---------------------------------------------------------------- 查询 + 租户隔离（§5.6）

def test_get_cross_tenant_404(client):
    client.post("/v1/operators", json=_body(), headers=_T1)
    r = client.get("/v1/operators/op.temp/1.0.0", headers=_T2)
    assert r.status_code == 404


def test_get_ok(client):
    client.post("/v1/operators", json=_body(), headers=_T1)
    r = client.get("/v1/operators/op.temp/1.0.0", headers=_T1)
    assert r.status_code == 200
    assert r.json()["operator"]["dsl_hash"]


def test_list_ok(client):
    client.post("/v1/operators", json=_body(), headers=_T1)
    r = client.get("/v1/operators", headers=_T1)
    assert r.status_code == 200
    assert r.json()["count"] == 1


# ---------------------------------------------------------------- 状态机（§5.3）

def _register(client, **kw):
    assert client.post("/v1/operators", json=_body(**kw), headers=_T1).status_code == 200


def test_transition_and_illegal(client):
    _register(client)
    r = client.post("/v1/operators/op.temp/1.0.0/transition",
                    json={"to": "Shadow"}, headers=_T1)
    assert r.status_code == 200
    assert r.json()["operator"]["state"] == "Shadow"
    # 非法：Shadow 不能直接到 Active
    r2 = client.post("/v1/operators/op.temp/1.0.0/transition",
                     json={"to": "Active", "evidence": {"metrics_ok": True}},
                     headers=_T1)
    assert r2.status_code == 422


def test_transition_missing_to_422(client):
    _register(client)
    r = client.post("/v1/operators/op.temp/1.0.0/transition", json={}, headers=_T1)
    assert r.status_code == 422


# ---------------------------------------------------------------- 灰度（§5.4）

def test_canary_flow(client):
    _register(client)
    client.post("/v1/operators/op.temp/1.0.0/transition",
                json={"to": "Shadow"}, headers=_T1)
    r = client.post("/v1/operators/op.temp/1.0.0/canary",
                    json={"percent": 30}, headers=_T1)
    assert r.status_code == 200
    op = r.json()["operator"]
    assert op["state"] == "Canary"
    assert op["rollout"]["percent"] == 30


def test_canary_bad_percent_422(client):
    _register(client)
    client.post("/v1/operators/op.temp/1.0.0/transition",
                json={"to": "Shadow"}, headers=_T1)
    r = client.post("/v1/operators/op.temp/1.0.0/canary",
                    json={"percent": 999}, headers=_T1)
    assert r.status_code == 422


# ---------------------------------------------------------------- 看门狗自动回滚（§5.5）

def test_watchdog_auto_rollback(client):
    _register(client)
    client.post("/v1/operators/op.temp/1.0.0/transition",
                json={"to": "Shadow"}, headers=_T1)
    client.post("/v1/operators/op.temp/1.0.0/canary", json={"percent": 20},
                headers=_T1)
    r = client.post("/v1/operators/op.temp/1.0.0/watchdog",
                    json={"canary": {"samples": 100, "crash_rate": 0.5},
                          "stable": {"samples": 100}}, headers=_T1)
    assert r.status_code == 200
    assert r.json()["rolled_back"] is True
    op = client.get("/v1/operators/op.temp/1.0.0", headers=_T1).json()["operator"]
    assert op["state"] == "RolledBack"


def test_watchdog_conservative_low_samples(client):
    _register(client)
    client.post("/v1/operators/op.temp/1.0.0/transition",
                json={"to": "Shadow"}, headers=_T1)
    client.post("/v1/operators/op.temp/1.0.0/canary", json={"percent": 20},
                headers=_T1)
    r = client.post("/v1/operators/op.temp/1.0.0/watchdog",
                    json={"canary": {"samples": 3, "crash_rate": 0.9},
                          "stable": {"samples": 100}}, headers=_T1)
    assert r.json()["rolled_back"] is False
