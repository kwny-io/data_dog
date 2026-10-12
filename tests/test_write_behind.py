"""write-behind（W3）：默认关闭零回归；开启后落库移出请求路径，边界必须可见。

为什么这些断言重要（docs/write_path_batching.md §10 的语义）
-----------------------------------------------------------
开了 write-behind，**HTTP 200 不再代表已落库**。所以这里钉住三件事：
1. **默认关闭**：start_writer_if_enabled() 返回 None，路由走原同步路径（零回归）。
2. **队列满 → 失败关闭**：submit() 返回 False，路由据此 503——绝不静默丢。
3. **后台写失败可见**：stats["failed"] 与 data_dog_write_behind_total{result="failed"}
   都 +1（响应已 200，这是唯一能发现丢数据的地方）。

约定：每个 test 结尾 print("PASS <name>")。
"""
from __future__ import annotations

import time

from data_dog import settings
from data_dog.service import write_behind as wb

_RAW = {
    "event_id": "e1", "occurred_at": "2026-10-12T00:00:00Z",
    "received_at": "2026-10-12T00:00:00.1Z", "sequence": 1,
    "vendor": "unitree", "model": "go2", "firmware": "3.2.1",
    "protocol_version": "motor-log-v2",
    "component_hint": "joint_motor.front_left_hip",
    "payload_inline": '{"temperature": 96, "current": 12.0, "mode": 0}',
}


def _tel(eid: str) -> dict:
    return {"event_id": eid, "device_sn": "d1", "occurred_at": "2026-10-12T00:00:00Z",
            "component_type": "joint_motor", "component_instance": "front_left_hip",
            "metrics": {"temperature": 96.0}, "status_codes": [],
            "operating_condition": "nominal"}


def _cand(cid: str) -> dict:
    return {"candidate_id": cid, "device_sn": "d1",
            "component_ref": "joint_motor.front_left_hip", "signal": "temperature_high",
            "severity_hint": "critical", "window_start": "2026-10-12T00:00:00Z",
            "window_end": "2026-10-12T00:00:00Z", "sample_count": 1,
            "rule_matches": ["MOTOR_TEMP_PROTECT"], "anomaly_score": 1.0,
            "evidence": [], "evidence_refs": [], "detector_provenance": "p2_demo@1"}


def _init_db(monkeypatch, tmp_path) -> None:
    url = "sqlite:///" + (tmp_path / "wb.db").as_posix()
    monkeypatch.setenv("DD_DATABASE_URL", url)
    from data_dog.storage.db import init_db
    init_db(url)


def _telemetry_count(tenant: str) -> int:
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    with session_scope() as s:
        return len(repo.list_telemetry(s, tenant_id=tenant))


def test_default_off(monkeypatch):
    monkeypatch.delenv("DD_WRITE_BEHIND", raising=False)
    assert settings.write_behind() is False
    assert wb.start_writer_if_enabled() is None
    print("PASS test_default_off")


def test_queue_full_fails_closed():
    w = wb.WriteBehindWriter(maxsize=1)          # 不起线程 → 没人消费
    assert w.submit(tenant_id="t", normalized=[_tel("e1")], candidates=[]) is True
    assert w.submit(tenant_id="t", normalized=[_tel("e2")], candidates=[]) is False
    assert w.stats["rejected_full"] == 1
    print("PASS test_queue_full_fails_closed")


def test_persists_submitted(monkeypatch, tmp_path):
    _init_db(monkeypatch, tmp_path)
    w = wb.WriteBehindWriter(maxsize=16)
    w.start()
    try:
        assert w.submit(tenant_id="t1", normalized=[_tel("e1")], candidates=[_cand("c1")])
        deadline = time.time() + 5
        while time.time() < deadline and _telemetry_count("t1") < 1:
            time.sleep(0.05)
        assert _telemetry_count("t1") == 1
        from data_dog.storage import repository as repo
        from data_dog.storage.db import session_scope
        with session_scope() as s:
            assert repo.get_candidate(s, "c1", tenant_id="t1") is not None
        assert w.stats["written"] == 1
    finally:
        w.stop(drain_timeout_s=5.0)
    print("PASS test_persists_submitted")


def test_worker_failure_is_visible(monkeypatch, tmp_path):
    _init_db(monkeypatch, tmp_path)
    from data_dog.storage import repository as repo

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "save_telemetry", boom)
    w = wb.WriteBehindWriter(maxsize=4)
    w.start()
    try:
        w.submit(tenant_id="t1", normalized=[_tel("e1")], candidates=[])
        deadline = time.time() + 5
        while time.time() < deadline and w.stats["failed"] == 0:
            time.sleep(0.05)
        assert w.stats["failed"] == 1
    finally:
        w.stop(drain_timeout_s=2.0)
    print("PASS test_worker_failure_is_visible")


def test_stop_drains_queue(monkeypatch, tmp_path):
    _init_db(monkeypatch, tmp_path)
    w = wb.WriteBehindWriter(maxsize=32)
    w.start()
    for i in range(5):
        w.submit(tenant_id="t1", normalized=[_tel(f"e{i}")], candidates=[])
    w.stop(drain_timeout_s=5.0)
    assert _telemetry_count("t1") == 5
    print("PASS test_stop_drains_queue")


def test_route_queues_when_enabled(monkeypatch, tmp_path):
    _init_db(monkeypatch, tmp_path)
    monkeypatch.setenv("DD_WRITE_BEHIND", "1")
    w = wb.WriteBehindWriter(maxsize=16)
    w.start()
    monkeypatch.setattr(wb, "_writer", w)
    try:
        from fastapi import Response

        from data_dog.service.routes.telemetry import ingest_telemetry
        resp = Response()
        out = ingest_telemetry({"events": [dict(_RAW)]}, resp,
                               {"tenant_id": "t1", "device_sn": "d1"})
        assert out["ok"] is True and out["persisted"]["mode"] == "write_behind"
        assert resp.status_code == 200
        deadline = time.time() + 5
        while time.time() < deadline and _telemetry_count("t1") < 1:
            time.sleep(0.05)
        assert _telemetry_count("t1") == 1
    finally:
        w.stop(drain_timeout_s=5.0)
    print("PASS test_route_queues_when_enabled")


def test_route_fails_closed_when_writer_missing(monkeypatch):
    monkeypatch.setenv("DD_WRITE_BEHIND", "1")
    monkeypatch.setattr(wb, "_writer", None)
    from fastapi import Response

    from data_dog.service.routes.telemetry import ingest_telemetry
    resp = Response()
    out = ingest_telemetry({"events": [dict(_RAW)]}, resp,
                           {"tenant_id": "t1", "device_sn": "d1"})
    assert out["ok"] is False and out["error"] == "write_behind_unavailable"
    assert resp.status_code == 503
    print("PASS test_route_fails_closed_when_writer_missing")
