# -*- coding: utf-8 -*-
"""在线服务层测试（FastAPI + TestClient）。

覆盖：存活/就绪探针、指标、鉴权失败关闭、接入→候选→落库、身份只认请求头、
查询过滤、批量上限。用临时文件 SQLite 隔离库。
"""
from __future__ import annotations

import json
import secrets

import pytest
from fastapi.testclient import TestClient

from data_dog.storage.db import init_db, reset_engine


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """把服务指向独立临时库，再起一个 TestClient（触发 startup 建表）。"""
    url = f"sqlite:///{(tmp_path / 'svc.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    reset_engine()
    init_db(url)

    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        yield c
    reset_engine()


def _raw(event_id="e1", temperature=95, **kw):
    base = dict(
        event_id=event_id, occurred_at="2026-09-13T10:00:00Z",
        received_at="2026-09-13T10:00:00.100Z", sequence=1,
        vendor="unitree", model="go2", firmware="3.2.1",
        protocol_version="motor-log-v2", component_hint="joint_motor.front_left_hip",
        payload_inline=json.dumps({"temperature": temperature, "current": 12.0, "mode": 0}),
    )
    base.update(kw)
    return base


_AUTH = {"X-Tenant-Id": "tenant-real", "X-Device-Sn": "device-go2-0001"}


# ---------------------------------------------------------------- 探针

def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readyz_reports_db(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"]["ok"] is True


def test_metrics_shape(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["resolver_ready"] is True
    assert "candidates_total" in body


# ---------------------------------------------------------------- 鉴权

def test_ingest_requires_identity_headers(client):
    r = client.post("/v1/telemetry", json={"events": [_raw()]})
    assert r.status_code == 401


def test_ingest_rejects_empty_events(client):
    r = client.post("/v1/telemetry", headers=_AUTH, json={"events": []})
    assert r.status_code == 422


def test_ingest_rejects_too_many_events(client):
    r = client.post("/v1/telemetry", headers=_AUTH,
                    json={"events": [_raw(f"e{i}") for i in range(1001)]})
    assert r.status_code == 422


# ---------------------------------------------------------------- 接入闭关

def test_ingest_produces_candidate_and_persists(client):
    r = client.post("/v1/telemetry", headers=_AUTH,
                    json={"events": [_raw("e1", temperature=96)]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["stats"]["ingested"] == 1
    assert body["stats"]["resolved"] == 1
    assert len(body["candidates"]) == 1
    assert body["candidates"][0]["severity_hint"] == "critical"
    assert body["persisted"]["candidates"] == 1

    # 落库可查
    q = client.get("/v1/candidates", headers=_AUTH)
    assert q.status_code == 200
    assert q.json()["count"] == 1


def test_body_identity_is_ignored(client):
    """正文谎报身份必须无效，实际身份以请求头为准。"""
    spoofed = _raw("e1", tenant_id="FAKE", device_sn="FAKE")
    r = client.post("/v1/telemetry", headers=_AUTH, json={"events": [spoofed]})
    assert r.status_code == 200
    items = client.get("/v1/candidates").json()["items"]
    assert len(items) == 1
    assert items[0]["device_sn"] == "device-go2-0001"


def test_fail_closed_on_bad_firmware(client):
    """固件不在兼容范围 → 失败关闭，仍返回 200 + 统计，但不产候选。"""
    r = client.post("/v1/telemetry", headers=_AUTH,
                    json={"events": [_raw("e1", firmware="9.9.9")]})
    assert r.status_code == 200
    body = r.json()
    assert body["stats"]["rejected"] == 1
    assert body["candidates"] == []


def test_candidates_filter_by_device(client):
    client.post("/v1/telemetry", headers=_AUTH, json={"events": [_raw("e1")]})
    hit = client.get("/v1/candidates", params={"device_sn": "device-go2-0001"}).json()
    miss = client.get("/v1/candidates", params={"device_sn": "nope"}).json()
    assert hit["count"] == 1 and miss["count"] == 0


def test_ingest_is_idempotent_across_repeats(client):
    """同一条日志重复接入，候选按 ID 幂等 upsert，不产生重复行。"""
    for _ in range(3):
        client.post("/v1/telemetry", headers=_AUTH, json={"events": [_raw("e1")]})
    assert client.get("/v1/candidates").json()["count"] == 1


# ---------------------------------------------------------------- 可选 API Key

def test_api_key_disabled_by_default(client):
    """不设 DD_API_KEY → 不校验（本地开发/评测行为不变）。"""
    assert client.get("/metrics").json()["auth_enabled"] is False
    assert client.get("/v1/candidates").status_code == 200


def test_api_key_enforced_when_set(client, monkeypatch):
    # 密钥在运行时随机生成，源码里不出现任何密钥字面量
    key = secrets.token_urlsafe(24)
    wrong = secrets.token_urlsafe(24)
    monkeypatch.setenv("DD_API_KEY", key)

    # 探针豁免：编排层探活不该被拦
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200

    # 业务端点：缺密钥 / 错密钥 → 401
    assert client.get("/v1/candidates").status_code == 401
    assert client.get("/v1/candidates", headers={"X-API-Key": wrong}).status_code == 401
    assert client.post("/v1/telemetry", headers=_AUTH,
                       json={"events": [_raw()]}).status_code == 401

    # 正确密钥 → 放行（业务头仍需齐全）
    ok = client.get("/v1/candidates", headers={"X-API-Key": key})
    assert ok.status_code == 200
    ingest = client.post("/v1/telemetry",
                         headers={**_AUTH, "X-API-Key": key},
                         json={"events": [_raw()]})
    assert ingest.status_code == 200
    assert ingest.json()["stats"]["candidates"] == 1
    # /metrics 属受保护端点（开启鉴权后同样要带密钥）
    assert client.get("/metrics").status_code == 401
    m = client.get("/metrics", headers={"X-API-Key": key}).json()
    assert m["auth_enabled"] is True


def test_blank_api_key_counts_as_unset(client, monkeypatch):
    """仅空白不算"已配置"——否则会误判为已开启鉴权。"""
    monkeypatch.setenv("DD_API_KEY", "   ")
    assert client.get("/metrics").json()["auth_enabled"] is False
    assert client.get("/v1/candidates").status_code == 200
