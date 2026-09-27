"""P3 端到端演示：调查结论 → 策略门 → 正式事件 → 投递 SaaS → 反馈闭环。

跑法：
    python -m data_dog.alerting.p3_demo          # 人读输出
    python -m data_dog.alerting.p3_demo --json   # 机器可读

演示内容：
  1. confirmed + 证据 + 厂商严重码 → 上报（critical），拿到 SaaS Receipt
  2. rejected（误报）→ 不上报
  3. 缺证据 → 失败关闭，不上报
  4. 同一 Episode 再次调查 → 同一 event_id，revision 递增（更新而非新建）
  5. 生命周期流转：confirmed → recovering → resolved → closed
  6. 人工反馈回填（误报/已修）
"""
from __future__ import annotations

import argparse
import json
import sys

from .saas import SaaSTestAdapter
from .service import AlertReportingService


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    svc = AlertReportingService(adapter=SaaSTestAdapter())
    log = []

    def _emit(tag, outcome):
        # 快照：event 对象会被后续状态流转原地更新，这里存展示所需的值
        log.append((tag, outcome, {
            "report": outcome.decision.report,
            "severity": outcome.event.severity if outcome.event else None,
            "state": outcome.event.state if outcome.event else None,
            "revision": outcome.event.event_revision if outcome.event else None,
            "event_id": outcome.event.event_id if outcome.event else None,
            "reasons": outcome.decision.reasons,
            "suppressed_reason": outcome.decision.suppressed_reason,
            "delivered": outcome.delivered,
            "receipt": outcome.receipt,
        }))
        return outcome

    r1 = _emit("confirmed+严重码", svc.process(
        {"investigation_id": "inv-001", "disposition": "confirmed", "confidence": 0.87,
         "severity": "warning", "summary": "左前髋关节电机温升异常，疑热退化",
         "recommended_actions": ["降低持续负载", "检查冷却路径"],
         "event_type": "joint_motor_thermal_degradation"},
        {"evidence_refs": ["metricwin://cand_01"], "vendor_severe_codes": ["OVERHEAT"],
         "candidate_severities": ["warning", "critical"]},
        device_ref="device-opaque-001", component_ref="joint_motor.front_left_hip"))

    _emit("rejected", svc.process(
        {"investigation_id": "inv-002", "disposition": "rejected", "confidence": 0.8,
         "summary": "电池 SOC 正常，误报"},
        {"evidence_refs": ["metricwin://cand_02"]},
        device_ref="device-opaque-001", component_ref="battery.main"))

    _emit("缺证据", svc.process(
        {"investigation_id": "inv-003", "disposition": "confirmed", "confidence": 0.9,
         "summary": "证据缺失样例"},
        {}, device_ref="device-opaque-001", component_ref="joint_motor.front_left_hip"))

    rev_before = r1.event.event_revision
    r4 = _emit("同Episode更新", svc.process(
        {"investigation_id": "inv-001", "disposition": "confirmed", "confidence": 0.92,
         "severity": "critical", "summary": "温度进一步升高，升级处理",
         "event_type": "joint_motor_thermal_degradation"},
        {"evidence_refs": ["metricwin://cand_01"], "candidate_severities": ["critical"]},
        device_ref="device-opaque-001", component_ref="joint_motor.front_left_hip"))

    ikey = r1.event.idempotency_key
    svc.transition(ikey, "recovering", reason="已降低负载")
    svc.transition(ikey, "resolved", reason="温度回落至基线")
    ev_closed = svc.transition(ikey, "closed", reason="确认恢复正常")
    fb = svc.adapter.submit_feedback(r1.event.event_id, "repaired", "更换轴承后恢复")

    if args.json:
        print(json.dumps({
            "steps": [{"tag": t, **o} for t, _outcome, o in log],
            "episode_revision_before": rev_before,
            "episode_revision_after": r4.event.event_revision,
            "final_state": ev_closed.state,
            "saas_received": len(svc.adapter.received),
            "outbox_stats": svc.outbox.stats,
            "feedback": fb,
        }, ensure_ascii=False, indent=2, default=str))
        return 0

    print("=" * 70)
    print("P3 上报闭环 —— 调查结论 → 策略门 → 正式事件 → SaaS")
    print("=" * 70)
    for tag, _outcome, o in log:
        verdict = "✅ 上报" if o["report"] else "⛔ 不上报"
        print(f"\n【{tag}】{verdict}")
        if o["report"]:
            print(f"   严重度={o['severity']}  状态={o['state']}  revision={o['revision']}")
            print(f"   event_id={o['event_id']}")
            print(f"   命中条件: {'; '.join(o['reasons'])}")
            print(f"   投递: {'成功' if o['delivered'] else '未投递'}  receipt={o['receipt']}")
        else:
            print(f"   抑制原因: {o['suppressed_reason']}")
    print("\n" + "-" * 70)
    r4_rev = log[-1][2]["revision"]
    print(f"Episode 更新：revision {rev_before} → {r4_rev}"
          f"（同一 event_id，非新建）")
    print(f"生命周期终态：{ev_closed.state}")
    print(f"人工反馈：{fb.get('kind')} — {fb.get('note')}")
    print(f"SaaS 收到事件数：{len(svc.adapter.received)}（无重复投递）")
    print(f"Outbox 统计：{svc.outbox.stats}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
