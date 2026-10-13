"""调查**运行日志**（回看用）：同步/异步都落、失败也落、租户隔离、通知带引用。

为什么单独一组
--------------
"技术人员事后调出运行日志发现问题"此前做不到：ReAct 轨迹只在**同步响应**里返回，异步
worker 路径直接丢弃，job 失败只留一个 error_code 枚举。这个能力一旦回退，值班人手上就
只剩"结论"没有"过程"，而排查恰恰要过程。故把它钉在这里（断言**内容**，不只看状态码）。
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from data_dog.alerting.notifiers import build_notification
from data_dog.storage.db import init_db, reset_engine

_AUTH = {"X-Tenant-Id": "tenant-real", "X-Device-Sn": "device-go2-0001"}
_Q = {"X-Tenant-Id": "tenant-real"}
_OTHER = {"X-Tenant-Id": "tenant-other"}


def _raw(event_id="e1", temperature=96, **kw):
    base = dict(
        event_id=event_id, occurred_at="2026-09-13T10:00:00Z",
        received_at="2026-09-13T10:00:00.100Z", sequence=1,
        vendor="unitree", model="go2", firmware="3.2.1",
        protocol_version="motor-log-v2",
        component_hint="joint_motor.front_left_hip",
        payload_inline=json.dumps(
            {"temperature": temperature, "current": 12.0, "mode": 0}),
    )
    base.update(kw)
    return base


def _client(tmp_path, monkeypatch, *, async_mode: bool):
    url = f"sqlite:///{(tmp_path / 'runlog.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    monkeypatch.setenv("DD_INVESTIGATE_OFFLINE", "1")
    if async_mode:
        monkeypatch.setenv("DD_INVESTIGATE_ASYNC", "1")
        monkeypatch.setenv("DD_INVESTIGATE_WORKERS", "1")
        monkeypatch.setenv("DD_INVESTIGATE_QUEUE_POLL_MS", "20")
    else:
        monkeypatch.delenv("DD_INVESTIGATE_ASYNC", raising=False)
    reset_engine()
    init_db(url)
    from data_dog.service.app import create_app
    return TestClient(create_app())


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, async_mode=False) as c:
        yield c
    reset_engine()


@pytest.fixture()
def async_client(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, async_mode=True) as c:
        yield c
    reset_engine()


def _seed_candidate(c, event_id="e1"):
    c.post("/v1/telemetry", headers=_AUTH, json={"events": [_raw(event_id)]})
    return c.get("/v1/candidates", headers=_Q).json()["items"][0]["candidate_id"]


# ---------------------------------------------------------------- 同步：成功

def test_sync_investigation_writes_readable_run_log(client):
    """同步调查：响应带 run_id=报告 id，且能直接按它调出**带步骤**的运行日志。"""
    cid = _seed_candidate(client)
    r = client.post("/v1/investigations", headers=_Q, json={"candidate_id": cid})
    assert r.status_code == 200, r.text
    body = r.json()
    iid = body["investigation_id"]
    assert body["run_id"] == iid, "同步路径的 run_id 必须就是 investigation_id（技术员手上是它）"

    log_r = client.get(f"/v1/investigations/{iid}/log", headers=_Q)
    assert log_r.status_code == 200, log_r.text
    log = log_r.json()["log"]
    assert log["status"] == "succeeded"
    assert log["candidate_id"] == cid
    assert log["step_count"] >= 1 and log["steps"], "轨迹不能是空的——那正是要调出来看的东西"
    names = [s.get("name") for s in log["steps"]]
    assert "query_candidate_evidence" in names, names
    assert log["usage"], "用量/耗时也要留（排查'慢在哪'）"
    assert isinstance(log["model"], str), log  # 离线档没有真模型 → 允许空串，但字段必须在
    assert log["duration_ms"] > 0, "耗时是排查'慢在哪'的第一手数字"


# ---------------------------------------------------------------- 同步：失败也留痕

def test_failed_sync_investigation_still_writes_run_log(client, monkeypatch):
    """同步调查失败（provider 起不来）→ HTTP 503，但运行日志**照样落**、带失败上下文。"""
    from fastapi import HTTPException

    from data_dog.service import runtime as rt

    def boom(*_a, **_k):
        raise HTTPException(status_code=503, detail="provider 暂时不可用")

    monkeypatch.setattr(rt, "_build_investigation_provider", boom)
    cid = _seed_candidate(client)
    r = client.post("/v1/investigations", headers=_Q, json={"candidate_id": cid})
    assert r.status_code == 503, r.text

    # 同步失败没有报告 → run_id 是本次尝试的 run_xxx，靠列表端点发现
    runs = client.get("/v1/investigations/runs", headers=_Q,
                      params={"candidate_id": cid, "status": "failed"}).json()
    assert runs["count"] == 1, runs
    item = runs["items"][0]
    assert item["run_id"].startswith("run_")
    assert item["error_code"] == "unavailable"
    assert "provider 暂时不可用" in item["error_detail"], item
    assert "steps" not in item, "列表页不该带 steps 正文（体积）"

    log = client.get(f"/v1/investigations/{item['run_id']}/log", headers=_Q).json()["log"]
    assert log["status"] == "failed" and log["steps"] == []


# ---------------------------------------------------------------- 异步：run_id 是 job_id

def test_async_investigation_run_log_is_keyed_by_job_id(async_client):
    """异步调查：run_id 就是 job_id（执行者才知道 job），用 jobs 端点调出。"""
    cid = _seed_candidate(async_client)
    job_id = async_client.post("/v1/investigations", headers=_Q,
                               json={"candidate_id": cid}).json()["job_id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = async_client.get(f"/v1/investigations/jobs/{job_id}", headers=_Q).json()["job"]
        if job["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.02)
    assert job["status"] == "succeeded", job

    log_r = async_client.get(f"/v1/investigations/jobs/{job_id}/log", headers=_Q)
    assert log_r.status_code == 200, log_r.text
    log = log_r.json()["log"]
    assert log["run_id"] == job_id
    assert log["kind"] == "async"
    assert log["report_id"] == job["report_id"]
    assert log["steps"], "异步路径此前**直接丢弃** trace，这里钉住不再丢"

    # 报告 id 上不该再挂一份异步日志（run_id 语义是二选一，不重复存）
    assert async_client.get(f"/v1/investigations/{job['report_id']}/log",
                            headers=_Q).status_code == 404


# ---------------------------------------------------------------- 租户隔离

def test_run_log_is_tenant_scoped(client):
    """跨租户读运行日志 → 404；别人的列表里也看不到。"""
    cid = _seed_candidate(client)
    iid = client.post("/v1/investigations", headers=_Q,
                      json={"candidate_id": cid}).json()["investigation_id"]
    assert client.get(f"/v1/investigations/{iid}/log", headers=_OTHER).status_code == 404
    # 注意：另一个租户连候选都看不到，故运行日志列表必然为空
    assert client.get("/v1/investigations/runs",
                      headers=_OTHER).json()["items"] == []


# ---------------------------------------------------------------- 通知带 run 引用

def test_notification_payload_carries_run_reference(monkeypatch):
    """告警通知要能把值班人带回诊断过程：investigation_id + 运行日志路径（配了基址给全链）。"""
    monkeypatch.setenv("DD_PUBLIC_BASE_URL", "https://dog.example.com/")
    note = build_notification({
        "event_id": "alert_x", "severity": "critical",
        "component_ref": "joint_motor.front_left_hip", "device_ref": "device-1",
        "summary": "温度持续超标", "recommended_actions": ["停机检查"],
        "investigation_id": "inv_abc123",
    })
    assert note.payload["investigation_id"] == "inv_abc123"
    assert note.payload["run_log_path"] == "/v1/investigations/inv_abc123/log"
    assert note.payload["run_log_url"] == \
        "https://dog.example.com/v1/investigations/inv_abc123/log"
    assert "运行日志" in note.body
    assert "inv_abc123" in note.body


def test_notification_without_investigation_does_not_fake_a_link(monkeypatch):
    """没有 investigation_id（老台账/未走调查）→ 不编假链接，两个字段都留空。"""
    monkeypatch.setenv("DD_PUBLIC_BASE_URL", "https://dog.example.com")
    note = build_notification({"event_id": "alert_y", "severity": "warning",
                               "component_ref": "c", "device_ref": "d", "summary": "s"})
    assert note.payload["investigation_id"] == ""
    assert note.payload["run_log_path"] == ""
    assert note.payload["run_log_url"] == ""
    assert "运行日志" not in note.body
