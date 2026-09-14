# -*- coding: utf-8 -*-
"""持久化层测试（SQLAlchemy）。

覆盖：建表 / 幂等 upsert / 重启不丢 / 查询过滤 / 各表往返。
用**临时文件 SQLite**（不是 :memory:），以真实模拟"进程重启后数据还在"。
"""
from __future__ import annotations

import pytest

from data_dog.alerting.lifecycle import AlertEvent
from data_dog.alerting.service import AlertReportingService
from data_dog.alerting.saas import SaaSTestAdapter
from data_dog.schemas.events import CandidateEvent, EvidenceSummary, InvestigationReport
from data_dog.storage import repository as repo
from data_dog.storage.db import init_db, session_scope


@pytest.fixture()
def db_url(tmp_path):
    """每个测试一个独立的临时 SQLite 文件库。"""
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    init_db(url)
    return url


def _cand(cid="cand_1"):
    return CandidateEvent(
        candidate_id=cid, device_sn="dev-1", component_ref="joint_motor.front_left_hip",
        signal="temperature_high", severity_hint="critical",
        window_start="2026-09-13T10:00:00Z", window_end="2026-09-13T10:00:04Z",
        sample_count=3, rule_matches=["MOTOR_TEMP_PROTECT"], anomaly_score=1.0,
        evidence=[EvidenceSummary(key="temperature", from_value=40, to_value=95)],
        evidence_refs=["metricwin://x"], detector_provenance="d@1.0")


# ---------------------------------------------------------------- 候选

def test_candidate_roundtrip(db_url):
    with session_scope(db_url) as s:
        assert repo.save_candidates(s, [_cand()]) == 1
    # 新会话（模拟重启）能读到，且字段完整
    with session_scope(db_url) as s:
        rows = repo.list_candidates(s)
    assert len(rows) == 1
    r = rows[0]
    assert r["candidate_id"] == "cand_1"
    assert r["rule_matches"] == ["MOTOR_TEMP_PROTECT"]
    assert r["evidence"][0]["to_value"] == 95


def test_candidate_save_is_idempotent(db_url):
    with session_scope(db_url) as s:
        repo.save_candidates(s, [_cand()])
        repo.save_candidates(s, [_cand()])      # 同 id 再存 → 更新而非重复
    with session_scope(db_url) as s:
        assert len(repo.list_candidates(s)) == 1


def test_candidate_filter(db_url):
    with session_scope(db_url) as s:
        repo.save_candidates(s, [_cand("c1")])
        c2 = _cand("c2")
        c2.device_sn = "dev-2"
        repo.save_candidates(s, [c2])
    with session_scope(db_url) as s:
        assert len(repo.list_candidates(s, device_sn="dev-1")) == 1
        assert len(repo.list_candidates(s, component_ref="joint_motor.front_left_hip")) == 2


# ---------------------------------------------------------------- 调查报告

def test_report_roundtrip(db_url):
    rep = InvestigationReport(investigation_id="inv_1", disposition="confirmed",
                              confidence=0.9, summary="热故障",
                              candidate_refs=["cand_1"],
                              recommended_actions=["降载"])
    with session_scope(db_url) as s:
        repo.save_report(s, rep)
    with session_scope(db_url) as s:
        rows = repo.list_reports(s)
    assert len(rows) == 1 and rows[0]["disposition"] == "confirmed"
    assert rows[0]["recommended_actions"] == ["降载"]


# ---------------------------------------------------------------- Alert + Outbox

def test_alert_roundtrip(db_url):
    ev = AlertEvent(event_id="alert_1", idempotency_key="dev:thermal:e1",
                    device_ref="dev-1", component_ref="joint_motor.front_left_hip",
                    event_type="motor_thermal", state="confirmed", severity="critical",
                    event_revision=2, summary="s")
    with session_scope(db_url) as s:
        repo.save_alert(s, ev)
    with session_scope(db_url) as s:
        got = repo.get_alert(s, "alert_1")
    assert got and got["state"] == "confirmed" and got["event_revision"] == 2


def test_outbox_idempotent_and_deliver(db_url):
    with session_scope(db_url) as s:
        assert repo.upsert_outbox(s, idempotency_key="k", revision=1, payload={"a": 1}) is True
        assert repo.upsert_outbox(s, idempotency_key="k", revision=1, payload={"a": 1}) is False
        assert repo.upsert_outbox(s, idempotency_key="k", revision=2, payload={"a": 2}) is True
    with session_scope(db_url) as s:
        assert len(repo.list_outbox(s)) == 2
        assert len(repo.list_outbox(s, status="pending")) == 2
        repo.mark_outbox_delivered(s, idempotency_key="k", revision=1, receipt="r1")
    with session_scope(db_url) as s:
        assert len(repo.list_outbox(s, status="delivered")) == 1


def test_feedback_roundtrip(db_url):
    with session_scope(db_url) as s:
        repo.save_feedback(s, event_id="alert_1", kind="repaired", note="换轴承")
    with session_scope(db_url) as s:
        fb = repo.list_feedback(s)
    assert fb and fb[0]["kind"] == "repaired"


# ---------------------------------------------------------------- 与 P3 集成

def test_p3_reporting_persisted(db_url):
    """P3 上报后，Alert 与 Outbox 能落库（模拟重启后仍可查）。"""
    svc = AlertReportingService(adapter=SaaSTestAdapter())
    outcome = svc.process(
        {"investigation_id": "inv_x", "disposition": "confirmed", "confidence": 0.9,
         "severity": "critical", "summary": "温升", "event_type": "motor_thermal"},
        {"evidence_refs": ["metricwin://x"], "vendor_severe_codes": ["OVERHEAT"]},
        device_ref="dev-1", component_ref="joint_motor.front_left_hip")
    assert outcome.delivered

    with session_scope(db_url) as s:
        repo.save_alert(s, outcome.event)
        repo.upsert_outbox(s, idempotency_key=outcome.event.idempotency_key,
                           revision=outcome.event.event_revision,
                           payload={"event_id": outcome.event.event_id})
    with session_scope(db_url) as s:
        assert repo.get_alert(s, outcome.event.event_id)["severity"] == "critical"
        assert len(repo.list_outbox(s)) == 1


# ---------------------------------------------------------------- 遥测

def test_telemetry_roundtrip(db_url):
    class _E:
        def __init__(self):
            self.event_id = "e1"
            self.tenant_id = "t1"
            self.device_sn = "dev-1"
            self.occurred_at = "2026-09-13T10:00:00Z"
            self.component_type = "joint_motor"
            self.component_instance = "front_left_hip"
            self.metrics = {"temperature": 95}
            self.status_codes = ["OVERHEAT"]
            self.operating_condition = "climbing"
            self.operator_id = "d"
            self.operator_version = "1.0"
            self.binding_revision = "b1"
            self.raw_log_ref = "rawlog://x"
    with session_scope(db_url) as s:
        assert repo.save_telemetry(s, [_E()]) == 1
    with session_scope(db_url) as s:
        rows = repo.list_telemetry(s)
    assert rows[0]["component_ref"] == "joint_motor.front_left_hip"
    assert rows[0]["metrics"]["temperature"] == 95


# ---------------------------------------------------------------- 数据库切换

def test_database_url_env_switch(monkeypatch, tmp_path):
    """DD_DATABASE_URL 决定连接串（本地 SQLite ↔ 生产 PostgreSQL 代码不改）。"""
    from data_dog.storage import db as dbmod
    monkeypatch.setenv("DD_DATABASE_URL", f"sqlite:///{(tmp_path/'x.db').as_posix()}")
    assert dbmod.database_url().startswith("sqlite:///")
    monkeypatch.setenv("DD_DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")
    assert dbmod.database_url().startswith("postgresql+psycopg://")
