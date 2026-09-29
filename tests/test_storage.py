"""持久化层测试（SQLAlchemy）。

覆盖：建表 / 幂等 upsert / 重启不丢 / 查询过滤 / 各表往返。
用**临时文件 SQLite**（不是 :memory:），以真实模拟"进程重启后数据还在"。
"""
from __future__ import annotations

import pytest

from data_dog.alerting.lifecycle import AlertEvent
from data_dog.alerting.saas import SaaSTestAdapter
from data_dog.alerting.service import AlertReportingService
from data_dog.schemas.events import CandidateEvent, EvidenceSummary, InvestigationReport
from data_dog.storage import repository as repo
from data_dog.storage.db import init_db, session_scope


@pytest.fixture()
def db_url(tmp_path):
    """每个测试一个独立的临时 SQLite 文件库。"""
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    init_db(url)
    return url


# 本文件所有往返测试使用同一租户；跨租户隔离另见下方专门用例。
T1 = "t1"
T2 = "t2"


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
        assert repo.save_candidates(s, [_cand()], tenant_id=T1) == 1
    # 新会话（模拟重启）能读到，且字段完整
    with session_scope(db_url) as s:
        rows = repo.list_candidates(s, tenant_id=T1)
    assert len(rows) == 1
    r = rows[0]
    assert r["candidate_id"] == "cand_1"
    assert r["rule_matches"] == ["MOTOR_TEMP_PROTECT"]
    assert r["evidence"][0]["to_value"] == 95


def test_candidate_save_is_idempotent(db_url):
    with session_scope(db_url) as s:
        repo.save_candidates(s, [_cand()], tenant_id=T1)
        repo.save_candidates(s, [_cand()], tenant_id=T1)  # 同 id 再存 → 更新而非重复
    with session_scope(db_url) as s:
        assert len(repo.list_candidates(s, tenant_id=T1)) == 1


def test_candidate_filter(db_url):
    with session_scope(db_url) as s:
        repo.save_candidates(s, [_cand("c1")], tenant_id=T1)
        c2 = _cand("c2")
        c2.device_sn = "dev-2"
        repo.save_candidates(s, [c2], tenant_id=T1)
    with session_scope(db_url) as s:
        assert len(repo.list_candidates(s, tenant_id=T1, device_sn="dev-1")) == 1
        assert len(repo.list_candidates(
            s, tenant_id=T1, component_ref="joint_motor.front_left_hip")) == 2


# ---------------------------------------------------------------- 调查报告

def test_report_roundtrip(db_url):
    rep = InvestigationReport(investigation_id="inv_1", disposition="confirmed",
                              confidence=0.9, summary="热故障",
                              candidate_refs=["cand_1"],
                              recommended_actions=["降载"])
    with session_scope(db_url) as s:
        repo.save_report(s, rep, tenant_id=T1)
    with session_scope(db_url) as s:
        rows = repo.list_reports(s, tenant_id=T1)
    assert len(rows) == 1 and rows[0]["disposition"] == "confirmed"
    assert rows[0]["recommended_actions"] == ["降载"]


def test_report_roundtrip_preserves_risk_window_and_links(db_url):
    """回归：`risk_window` / `counter_evidence_refs` / `linked_candidate_refs`
    曾被 `save_report` 静默丢弃（表无列）。守住这三项往返不丢。"""
    rep = InvestigationReport(
        investigation_id="inv_2", disposition="confirmed", confidence=0.8,
        summary="s", risk_window="24 小时内检查",
        counter_evidence_refs=["evidence://cand_2/torque"],
        linked_candidate_refs=["cand_2", "cand_3"],
    )
    with session_scope(db_url) as s:
        repo.save_report(s, rep, tenant_id=T1)
    with session_scope(db_url) as s:
        got = repo.get_report(s, "inv_2", tenant_id=T1)
    assert got is not None
    assert got["risk_window"] == "24 小时内检查"
    assert got["counter_evidence_refs"] == ["evidence://cand_2/torque"]
    assert got["linked_candidate_refs"] == ["cand_2", "cand_3"]


# ---------------------------------------------------------------- Alert + Outbox

def test_alert_roundtrip(db_url):
    ev = AlertEvent(event_id="alert_1", idempotency_key="dev:thermal:e1",
                    device_ref="dev-1", component_ref="joint_motor.front_left_hip",
                    event_type="motor_thermal", state="confirmed", severity="critical",
                    event_revision=2, summary="s")
    with session_scope(db_url) as s:
        repo.save_alert(s, ev, tenant_id=T1)
    with session_scope(db_url) as s:
        got = repo.get_alert(s, "alert_1", tenant_id=T1)
    assert got and got["state"] == "confirmed" and got["event_revision"] == 2


def test_outbox_idempotent_and_deliver(db_url):
    with session_scope(db_url) as s:
        assert repo.upsert_outbox(s, tenant_id=T1, idempotency_key="k",
                                  revision=1, payload={"a": 1}) is True
        assert repo.upsert_outbox(s, tenant_id=T1, idempotency_key="k",
                                  revision=1, payload={"a": 1}) is False
        assert repo.upsert_outbox(s, tenant_id=T1, idempotency_key="k",
                                  revision=2, payload={"a": 2}) is True
    with session_scope(db_url) as s:
        assert len(repo.list_outbox(s, tenant_id=T1)) == 2
        assert len(repo.list_outbox(s, tenant_id=T1, status="pending")) == 2
        repo.mark_outbox_delivered(s, tenant_id=T1, idempotency_key="k",
                                   revision=1, receipt="r1")
    with session_scope(db_url) as s:
        assert len(repo.list_outbox(s, tenant_id=T1, status="delivered")) == 1


def test_feedback_roundtrip(db_url):
    with session_scope(db_url) as s:
        repo.save_feedback(s, tenant_id=T1, event_id="alert_1", kind="repaired",
                           note="换轴承")
    with session_scope(db_url) as s:
        fb = repo.list_feedback(s, tenant_id=T1)
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
        repo.save_alert(s, outcome.event, tenant_id=T1)
        repo.upsert_outbox(s, tenant_id=T1,
                           idempotency_key=outcome.event.idempotency_key,
                           revision=outcome.event.event_revision,
                           payload={"event_id": outcome.event.event_id})
    with session_scope(db_url) as s:
        assert repo.get_alert(s, outcome.event.event_id,
                              tenant_id=T1)["severity"] == "critical"
        assert len(repo.list_outbox(s, tenant_id=T1)) == 1


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
        # 租户来自调用方（认证上下文），正文里的 tenant_id="t1" 被忽略
        assert repo.save_telemetry(s, [_E()], tenant_id=T2) == 1
    with session_scope(db_url) as s:
        rows = repo.list_telemetry(s, tenant_id=T2)
    assert rows[0]["component_ref"] == "joint_motor.front_left_hip"
    assert rows[0]["metrics"]["temperature"] == 95
    # 正文自称 t1，但落库归属 t2；t1 查不到（正文身份不可信）
    assert rows[0]["tenant_id"] == T2
    with session_scope(db_url) as s:
        assert repo.list_telemetry(s, tenant_id=T1) == []


# ---------------------------------------------------------------- 数据库切换

def test_database_url_env_switch(monkeypatch, tmp_path):
    """DD_DATABASE_URL 决定连接串（本地 SQLite ↔ 生产 PostgreSQL 代码不改）。"""
    from data_dog.storage import db as dbmod
    monkeypatch.setenv("DD_DATABASE_URL", f"sqlite:///{(tmp_path/'x.db').as_posix()}")
    assert dbmod.database_url().startswith("sqlite:///")
    monkeypatch.setenv("DD_DATABASE_URL", "postgresql+psycopg://u:p@h:5432/d")
    assert dbmod.database_url().startswith("postgresql+psycopg://")


# ---------------------------------------------------------------- 并发安全

def test_concurrent_upsert_same_id_is_idempotent(db_url):
    """并发写同一主键不得抛 IntegrityError。

    回归测试：`save_candidates` 曾是 get-then-add（read-modify-write），
    并发的两个线程会同时 get 到 None、各自 INSERT 同一主键，后者 flush 时
    抛 IntegrityError（实测 8 并发下 2 个抛错）。在 HTTP 服务里这会被兜底成
    `persist_failed`(207)，把一次**有效写入**误报为失败。
    """
    import threading

    errs, ok = [], []

    def worker(i):
        try:
            with session_scope(db_url) as s:
                # 8 个线程写**同一个** candidate_id，制造主键竞争
                repo.save_candidates(s, [_cand("cand-race-same")], tenant_id=T1)
            ok.append(i)
        except Exception as e:                      # noqa: BLE001
            errs.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errs == [], f"并发 upsert 不应抛异常，实际: {errs}"
    assert len(ok) == 8
    with session_scope(db_url) as s:
        rows = repo.list_candidates(s, tenant_id=T1)
    assert len(rows) == 1, "同一主键并发写入应只留一条记录"


# ---------------------------------------------------------------- 多租户隔离

def test_tenant_isolation_reads(db_url):
    """t1 写入的数据，t2 读不到（行级隔离）。"""
    with session_scope(db_url) as s:
        repo.save_candidates(s, [_cand("c1")], tenant_id=T1)
        repo.save_alert(s, AlertEvent(
            event_id="alert_1", idempotency_key="k", device_ref="dev-1",
            component_ref="c", event_type="e", state="detected"), tenant_id=T1)
    with session_scope(db_url) as s:
        assert len(repo.list_candidates(s, tenant_id=T1)) == 1
        assert repo.list_candidates(s, tenant_id=T2) == []
        assert repo.get_candidate(s, "c1", tenant_id=T1) is not None
        assert repo.get_candidate(s, "c1", tenant_id=T2) is None
        assert repo.get_alert(s, "alert_1", tenant_id=T2) is None


def test_tenant_isolation_no_pk_collision(db_url):
    """同一 candidate_id 在两个租户下各存一行，互不覆盖。

    这是复合主键的核心意义：确定性 candidate_id 不含租户，若主键只有
    candidate_id，后写入的租户会静默覆盖前一个租户的行。
    """
    with session_scope(db_url) as s:
        repo.save_candidates(s, [_cand("same")], tenant_id=T1)
    c2 = _cand("same")
    c2.device_sn = "dev-2"
    with session_scope(db_url) as s:
        repo.save_candidates(s, [c2], tenant_id=T2)
    with session_scope(db_url) as s:
        r1 = repo.get_candidate(s, "same", tenant_id=T1)
        r2 = repo.get_candidate(s, "same", tenant_id=T2)
    assert r1["device_sn"] == "dev-1"
    assert r2["device_sn"] == "dev-2", "t2 的写入覆盖了 t1 的行（主键未含租户）"
