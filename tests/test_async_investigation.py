"""异步调查端点测试（G1）。

覆盖：提交即返回 202、状态流转 pending→running→succeeded、报告可读、
失败落终态不 5xx、取消、多 worker 不重复执行（条件领取的原子性）、
租户隔离、以及**同步路径零回归**（DD_INVESTIGATE_ASYNC 未开时行为不变）。

用临时文件 SQLite，离线确定性 provider（不调真实 LLM）。
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from data_dog.storage.db import init_db, reset_engine

_AUTH = {"X-Tenant-Id": "tenant-real", "X-Device-Sn": "device-go2-0001"}
_Q = {"X-Tenant-Id": "tenant-real"}


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


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """异步模式下的服务：DD_INVESTIGATE_ASYNC=1 起 worker 池。"""
    url = f"sqlite:///{(tmp_path / 'async.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    monkeypatch.setenv("DD_INVESTIGATE_OFFLINE", "1")
    monkeypatch.setenv("DD_INVESTIGATE_ASYNC", "1")
    monkeypatch.setenv("DD_INVESTIGATE_WORKERS", "2")
    monkeypatch.setenv("DD_INVESTIGATE_QUEUE_POLL_MS", "20")
    reset_engine()
    init_db(url)

    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        yield c
    reset_engine()


def _seed_candidate(client, event_id="e1", temperature=96):
    client.post("/v1/telemetry", headers=_AUTH,
                json={"events": [_raw(event_id, temperature=temperature)]})
    items = client.get("/v1/candidates", headers=_Q).json()["items"]
    return items[0]["candidate_id"]


def _wait_status(client, job_id, want, timeout_s=10.0):
    """轮询 job 直到到达 want 之一（集合或单值）。返回 job dict。"""
    wants = {want} if isinstance(want, str) else set(want)
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        r = client.get(f"/v1/investigations/jobs/{job_id}", headers=_Q)
        assert r.status_code == 200, r.text
        last = r.json()["job"]
        if last["status"] in wants:
            return last
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} 未在 {timeout_s}s 内到达 {wants}，"
                         f"最后状态 {last}")


# ---------------------------------------------------------------- 提交即返回

def test_submit_returns_202_immediately(client):
    """POST 立即返回 202 + job_id（不阻塞等调查完）。"""
    cid = _seed_candidate(client)
    t0 = time.monotonic()
    r = client.post("/v1/investigations", headers=_Q,
                    json={"candidate_id": cid})
    elapsed = time.monotonic() - t0
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["job_id"].startswith("job_")
    assert body["status"] == "pending"
    assert body["poll"] == f"/v1/investigations/jobs/{body['job_id']}"
    assert elapsed < 2.0, f"提交不应阻塞（实测 {elapsed:.2f}s）"


def test_state_flow_to_succeeded(client):
    """状态流转 pending→running→succeeded；succeeded 后 report_id 可读。"""
    cid = _seed_candidate(client)
    job_id = client.post("/v1/investigations", headers=_Q,
                         json={"candidate_id": cid}).json()["job_id"]

    job = _wait_status(client, job_id, "succeeded")
    assert job["report_id"], "succeeded 必须有 report_id"
    assert job["candidate_id"] == cid
    assert job["started_at"] and job["finished_at"]

    # 报告可直接读回（免重跑 LLM）
    r = client.get(f"/v1/investigations/{job['report_id']}", headers=_Q)
    assert r.status_code == 200
    assert r.json()["report"]["investigation_id"] == job["report_id"]


def test_unknown_candidate_404(client):
    r = client.post("/v1/investigations", headers=_Q,
                    json={"candidate_id": "cand_nope"})
    assert r.status_code == 404


def test_requires_candidate_id_and_tenant(client):
    assert client.post("/v1/investigations", headers=_Q,
                       json={}).status_code == 422
    assert client.post("/v1/investigations",
                       json={"candidate_id": "x"}).status_code == 401


# ---------------------------------------------------------------- 失败落终态

def test_failed_job_has_error_code(client, monkeypatch):
    """provider 构造失败（无凭据）→ job failed + 有限错误码，服务不 5xx。

    不依赖"环境里没有 key"（本仓库 .env 有真实 key）——直接把 provider 构造点
    monkeypatch 成抛 503，确定性地走失败分支（worker 在另一线程按模块名解析它）。
    """
    import sys

    from fastapi import HTTPException
    # 构造点在 `service/runtime.py`（拆分后 `service/app.py` 只做装配），
    # 故 patch 目标要跟着走——patch 到 app 的命名空间不会影响 runtime 的调用。
    mod = sys.modules["data_dog.service.runtime"]
    monkeypatch.setattr(mod, "_build_investigation_provider",
                        lambda should_cancel=None: (_ for _ in ()).throw(
                            HTTPException(status_code=503, detail="no credential")))

    cid = _seed_candidate(client)
    job_id = client.post("/v1/investigations", headers=_Q,
                         json={"candidate_id": cid}).json()["job_id"]
    job = _wait_status(client, job_id, "failed")
    assert job["error_code"], "失败必须带有限错误码"
    assert job["report_id"] is None


# ---------------------------------------------------------------- 取消

def test_cancel_job_is_terminal(client):
    """取消一个 job → 状态为 cancelled；终态不可被 worker 覆盖回 succeeded。"""
    cid = _seed_candidate(client)
    job_id = client.post("/v1/investigations", headers=_Q,
                         json={"candidate_id": cid}).json()["job_id"]
    r = client.post(f"/v1/investigations/jobs/{job_id}/cancel", headers=_Q)
    assert r.status_code == 200, r.text
    assert r.json()["job"]["status"] == "cancelled"

    # 无论 worker 此前是否已开跑，cancelled 都是终态：等一下确认不被覆盖
    time.sleep(0.3)
    job = client.get(f"/v1/investigations/jobs/{job_id}", headers=_Q).json()["job"]
    assert job["status"] == "cancelled"


def test_cancel_unknown_404(client):
    r = client.post("/v1/investigations/jobs/job_nope/cancel", headers=_Q)
    assert r.status_code == 404


# ---------------------------------------------------------------- 租户隔离

def test_job_cross_tenant_404(client):
    cid = _seed_candidate(client)
    job_id = client.post("/v1/investigations", headers=_Q,
                         json={"candidate_id": cid}).json()["job_id"]
    r = client.get(f"/v1/investigations/jobs/{job_id}",
                   headers={"X-Tenant-Id": "tenant-other"})
    assert r.status_code == 404
    r2 = client.post(f"/v1/investigations/jobs/{job_id}/cancel",
                     headers={"X-Tenant-Id": "tenant-other"})
    assert r2.status_code == 404


# ---------------------------------------------------------------- 条件领取原子性

def test_claim_job_is_atomic(tmp_path, monkeypatch):
    """两个并发 worker 抢同一 pending job → 只有一个领取成功。

    这是"多副本不重复调查"的核心保证：`UPDATE ... WHERE status='pending'` 的条件
    更新在数据库层保证只有一个 rowcount=1。
    """
    import threading
    url = f"sqlite:///{(tmp_path / 'claim.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    reset_engine()
    init_db(url)

    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope

    with session_scope() as s:
        repo.create_job(s, tenant_id="t1", job_id="job_x", candidate_id="c1")

    results: list[bool] = []
    barrier = threading.Barrier(2)

    def worker(wid):
        barrier.wait()
        with session_scope(tenant_id="t1") as s:
            results.append(repo.claim_job(s, "job_x", tenant_id="t1",
                                          worker_id=wid))
    ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results.count(True) == 1, f"应恰有一个领取成功，实际 {results}"
    assert results.count(False) == 1


def test_finish_job_wont_overwrite_cancelled(tmp_path, monkeypatch):
    """取消后 worker 想写 succeeded → 被拒（cancelled 不被覆盖）。"""
    url = f"sqlite:///{(tmp_path / 'cancel2.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    reset_engine()
    init_db(url)

    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope

    with session_scope() as s:
        repo.create_job(s, tenant_id="t1", job_id="job_y", candidate_id="c1")
    with session_scope(tenant_id="t1") as s:
        assert repo.claim_job(s, "job_y", tenant_id="t1", worker_id="w0") is True
    with session_scope(tenant_id="t1") as s:
        assert repo.cancel_job(s, "job_y", tenant_id="t1") == "cancelled"
    with session_scope(tenant_id="t1") as s:
        # job 已是 cancelled，不再满足 status='running' 的条件更新 → False
        assert repo.finish_job(s, "job_y", tenant_id="t1",
                               status="succeeded", report_id="inv_x") is False
    with session_scope() as s:
        row = repo.get_job(s, "job_y", tenant_id="t1")
    assert row["status"] == "cancelled"
    assert row["report_id"] is None


# ---------------------------------------------------------------- 列表 / 就绪

def test_list_jobs(client):
    cid = _seed_candidate(client)
    for _ in range(2):
        client.post("/v1/investigations", headers=_Q,
                    json={"candidate_id": cid})
    _wait_status(client, client.post("/v1/investigations", headers=_Q,
                                     json={"candidate_id": cid}).json()["job_id"],
                 "succeeded")
    body = client.get("/v1/investigations/jobs", headers=_Q).json()
    assert body["ok"] is True
    assert body["total"] >= 3
    # 可按状态过滤
    succ = client.get("/v1/investigations/jobs", headers=_Q,
                      params={"status": "succeeded"}).json()
    assert all(j["status"] == "succeeded" for j in succ["items"])


def test_readyz_reports_workers(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["checks"]["investigation_workers"]["ok"] is True


# ---------------------------------------------------------------- 同步路径零回归

def test_sync_path_unchanged_when_async_off(tmp_path, monkeypatch):
    """DD_INVESTIGATE_ASYNC 未开 → POST 走同步，返回 200 + 完整报告（旧契约）。"""
    url = f"sqlite:///{(tmp_path / 'sync.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    monkeypatch.setenv("DD_INVESTIGATE_OFFLINE", "1")
    monkeypatch.delenv("DD_INVESTIGATE_ASYNC", raising=False)
    reset_engine()
    init_db(url)

    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        cid = _seed_candidate(c)
        r = c.post("/v1/investigations", headers=_Q,
                   json={"candidate_id": cid})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["investigation_id"]
        assert body["report"]["investigation_id"] == body["investigation_id"]
        assert body["trace"]

        # ?sync=1 在异步开启时也应退回同步
        r2 = c.post("/v1/investigations", headers=_Q, params={"sync": 1},
                    json={"candidate_id": cid})
        assert r2.status_code == 200
    reset_engine()
