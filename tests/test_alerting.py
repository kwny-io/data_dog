# -*- coding: utf-8 -*-
"""P3 上报闭环测试：策略门 / 生命周期 / 幂等 outbox / SaaS 适配器 / 端到端。

覆盖：
  - 策略门：四条件、冲突消解（取最高严重度）、rejected 不上报、缺证据失败关闭
  - 生命周期：合法/非法转移、event_id 复用 + revision 递增、Suppressed/Resolved 回流
  - 幂等键构成规则 + outbox 去重 + 重试/死信
  - SaaS 适配器：幂等 Receipt、禁止字段剔除、反馈
  - 端到端：confirmed→上报、同 Episode→更新不重复、状态流转到 Closed
"""
from __future__ import annotations

import pytest

from data_dog.alerting.lifecycle import (
    AlertEvent, AlertLifecycle, AlertTransitionError,
)
from data_dog.alerting.outbox import (
    AlertOutbox, episode_key_from, make_idempotency_key,
)
from data_dog.alerting.policy import ReportPolicyGate
from data_dog.alerting.saas import SaaSTestAdapter
from data_dog.alerting.service import AlertReportingService


# ---------------------------------------------------------------- 策略门

def test_policy_condition_vendor_severe():
    d = ReportPolicyGate().evaluate(
        {"disposition": "needs_human_review", "confidence": 0.3},
        {"evidence_refs": ["x"], "vendor_severe_codes": ["OVERHEAT"]})
    assert d.report and d.severity == "critical"


def test_policy_condition_confirmed_confidence():
    d = ReportPolicyGate().evaluate(
        {"disposition": "confirmed", "confidence": 0.8, "severity": "warning"},
        {"evidence_refs": ["x"]})
    assert d.report and any("条件③" in r for r in d.reasons)


def test_policy_confirmed_below_threshold_not_reported():
    d = ReportPolicyGate().evaluate(
        {"disposition": "confirmed", "confidence": 0.5}, {"evidence_refs": ["x"]})
    assert not d.report


def test_policy_rejected_not_reported():
    d = ReportPolicyGate().evaluate(
        {"disposition": "rejected", "confidence": 0.9}, {"evidence_refs": ["x"]})
    assert not d.report and "rejected" in d.suppressed_reason


def test_policy_evidence_missing_fail_closed():
    d = ReportPolicyGate().evaluate({"disposition": "confirmed", "confidence": 0.9}, {})
    assert not d.report and "证据" in d.suppressed_reason


def test_policy_severity_conflict_takes_max():
    d = ReportPolicyGate().evaluate(
        {"disposition": "confirmed", "confidence": 0.9, "severity": "info"},
        {"evidence_refs": ["x"], "candidate_severities": ["warning", "critical"]})
    assert d.severity == "critical"


def test_policy_multi_window():
    d = ReportPolicyGate().evaluate(
        {"disposition": "needs_human_review", "confidence": 0.2},
        {"evidence_refs": ["x"], "window_hits": 3})
    assert d.report and any("条件②" in r for r in d.reasons)


# ---------------------------------------------------------------- 生命周期

def _lc():
    return AlertLifecycle(AlertEvent(
        event_id="alert_x", idempotency_key="k", device_ref="d",
        component_ref="joint_motor.front_left_hip", event_type="t"))


def test_lifecycle_happy_path():
    lc = _lc()
    assert lc.event.state == "detected"
    lc.transition("confirmed")
    lc.transition("escalated")
    lc.transition("recovering")
    lc.transition("resolved")
    lc.transition("closed")
    assert lc.event.state == "closed"
    assert lc.event.event_revision == 6      # 初始 1 + 5 次转移


def test_lifecycle_illegal_transition_rejected():
    lc = _lc()
    with pytest.raises(AlertTransitionError, match="不能直接到"):
        lc.transition("closed")              # detected 不能直接 closed
    with pytest.raises(AlertTransitionError, match="未知状态"):
        lc.transition("bogus")
    assert lc.event.state == "detected"      # 未变


def test_lifecycle_suppressed_can_redetect():
    lc = _lc()
    lc.transition("suppressed")
    lc.transition("detected")                # 条件恶化回流
    assert lc.event.state == "detected"


def test_lifecycle_active_states():
    lc = _lc()
    assert lc.is_active()                    # detected 算活动
    lc.transition("confirmed")
    assert lc.is_active()
    lc.transition("closed")
    assert not lc.is_active()


# ---------------------------------------------------------------- 幂等键 / outbox

def test_idempotency_key_rules():
    k = make_idempotency_key("device-opaque", "joint_motor_thermal_degradation", "inv1")
    assert k == "device-opaque:joint_motor_thermal_degradation:inv1"
    assert episode_key_from("inv1", "comp") == "inv1"
    assert episode_key_from("", "comp") == "comp"
    with pytest.raises(ValueError):
        make_idempotency_key("", "t", "e")


def test_outbox_dedup_same_key_revision():
    ob = AlertOutbox()
    a = ob.enqueue("k", 1, {"x": 1})
    b = ob.enqueue("k", 1, {"x": 1})
    assert a.dedup_id == b.dedup_id
    assert ob.stats["duplicate"] == 1
    assert len(ob) == 1
    ob.enqueue("k", 2, {"x": 2})             # 同键不同 revision → 不同条目
    assert len(ob) == 2


def test_outbox_retry_then_dead():
    ad = SaaSTestAdapter(fail_times=99)
    ob = AlertOutbox(max_attempts=3)
    ob.enqueue("k", 1, {"x": 1})
    last = None
    for _ in range(3):
        last = ob.deliver(ad)
    entry = ob.list()[0]
    assert entry["status"] == "dead" and entry["attempts"] == 3


# ---------------------------------------------------------------- SaaS 适配器

def test_saas_idempotent_receipt():
    ad = SaaSTestAdapter()
    ok1, r1 = ad.deliver({"a": 1}, idempotency_key="k", revision=1)
    ok2, r2 = ad.deliver({"a": 1}, idempotency_key="k", revision=1)
    assert ok1 and ok2 and r1["receipt_id"] == r2["receipt_id"]
    assert r2["duplicate"] is True and len(ad.received) == 1


def test_saas_scrubs_forbidden_fields():
    ad = SaaSTestAdapter()
    ad.deliver({"payload_ref": "rawlog://x", "summary": "ok", "nested": {"secret": "s"}},
               idempotency_key="k", revision=1)
    rec = ad.received[0]
    assert "payload_ref" not in rec and "secret" not in rec.get("nested", {})
    assert rec["summary"] == "ok"


def test_saas_feedback():
    ad = SaaSTestAdapter()
    assert ad.submit_feedback("alert_x", "false_positive")["ok"]
    assert not ad.submit_feedback("alert_x", "bogus")["ok"]
    assert ad.feedback[0]["kind"] == "false_positive"


# ---------------------------------------------------------------- 端到端

def test_service_end_to_end_and_episode_update():
    svc = AlertReportingService(adapter=SaaSTestAdapter())
    r1 = svc.process(
        {"investigation_id": "inv1", "disposition": "confirmed", "confidence": 0.87,
         "severity": "warning", "summary": "温升", "event_type": "motor_thermal"},
        {"evidence_refs": ["metricwin://x"], "vendor_severe_codes": ["OVERHEAT"],
         "candidate_severities": ["warning", "critical"]},
        device_ref="device-go2-0001", component_ref="joint_motor.front_left_hip")
    assert r1.decision.report and r1.delivered and r1.event.state == "confirmed"
    assert r1.decision.severity == "critical"
    rev1 = r1.event.event_revision
    event_id_1 = r1.event.event_id

    # 同 Episode 再来一次 → event_id 不变，revision 递增，不新建
    # （注意：event 是同一对象被原地更新，故先快照 rev1）
    r2 = svc.process(
        {"investigation_id": "inv1", "disposition": "confirmed", "confidence": 0.9,
         "severity": "critical", "summary": "更高", "event_type": "motor_thermal"},
        {"evidence_refs": ["metricwin://x"]},
        device_ref="device-go2-0001", component_ref="joint_motor.front_left_hip")
    assert r2.event.event_id == event_id_1
    assert r2.event.event_revision > rev1
    assert len(svc.adapter.received) == 2       # 只投递了 2 个 revision，无重复事件


def test_service_rejected_not_delivered():
    svc = AlertReportingService(adapter=SaaSTestAdapter())
    r = svc.process({"investigation_id": "inv2", "disposition": "rejected",
                     "confidence": 0.9, "summary": "误报"},
                    {"evidence_refs": ["x"]}, device_ref="d", component_ref="battery.main")
    assert not r.decision.report and r.event is None
    assert len(svc.adapter.received) == 0


def test_service_episode_transitions_to_closed():
    svc = AlertReportingService(adapter=SaaSTestAdapter())
    r = svc.process({"investigation_id": "inv3", "disposition": "confirmed",
                     "confidence": 0.9, "summary": "s", "event_type": "t"},
                    {"evidence_refs": ["x"]}, device_ref="d", component_ref="c.main")
    ikey = r.event.idempotency_key
    svc.transition(ikey, "recovering", reason="已处置")
    svc.transition(ikey, "resolved", reason="温度回落")
    ev = svc.transition(ikey, "closed", reason="确认恢复")
    assert ev.state == "closed"
    assert svc.active_events() == {}            # 不再有活动事件
