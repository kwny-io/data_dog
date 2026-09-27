"""A3：提出「发布 / 回滚 / 上报」意图（架构 13.2 write 类工具的最小落地）。

验证：
- 三个 propose_* 都能写入本地 outbox，并返回带 receipt_id 的 Receipt。
- 同 idempotency_key 重复提出 → 返回同一条 Receipt，duplicate 标记 True（幂等）。
- AgentLoop 调查出 needs_human_review/confirmed 时，自动提出上报意图并附在报告上；
  rejected（误报）不上报。
"""
import json

from data_dog.agent.loop import AgentLoop
from data_dog.evidence.store import EvidenceStore
from data_dog.reporter import (
    ProposeOutbox,
    propose_alert_report,
    propose_operator_release,
    propose_operator_rollback,
)
from data_dog.schemas.events import CandidateEvent
from data_dog.tools.investigation import InvestigationTools


def test_propose_idempotent_receipt():
    box = ProposeOutbox()

    # 发布意图
    r1 = propose_operator_release("vendor-a.motor-parser@1.3.0", "canary",
                                  "固件升级验证通过", outbox=box)
    assert r1["ok"] is True
    assert r1["receipt_id"].startswith("receipt_")
    assert r1["status"] == "acknowledged"
    assert r1["duplicate"] is False

    # 同幂等键重复 → 同 receipt，duplicate=True
    r1b = propose_operator_release("vendor-a.motor-parser@1.3.0", "canary",
                                   "再提一次", outbox=box)
    assert r1b["receipt_id"] == r1["receipt_id"]
    assert r1b["duplicate"] is True

    # 回滚意图
    r2 = propose_operator_rollback("vendor-a.motor-parser@1.3.0", "指标退化", outbox=box)
    assert r2["ok"] is True and r2["receipt_id"] != r1["receipt_id"]

    # 上报意图
    r3 = propose_alert_report("inv_c-hip", "左髋热退化", "warning", outbox=box)
    assert r3["ok"] is True and r3["receipt_id"].startswith("receipt_")

    # 关键：一个智能提出 3 条不同意图，outbox 里就 3 条（无重复）
    assert len(box) == 3
    print("PASS test_propose_idempotent_receipt")


def _cand(cid: str, comp: str) -> CandidateEvent:
    return CandidateEvent(
        candidate_id=cid, device_sn="device-abc123", component_ref=comp,
        signal="temperature_rising_under_normalized_load", severity_hint="warning",
        window_start="09:55", window_end="10:00", sample_count=300,
        rule_matches=["T"], anomaly_score=0.9, detector_provenance="test",
    )


class FakeProvider:
    """查一次证据，再给一个可配置的裁决结论。"""

    def __init__(self, disposition: str):
        self.disposition = disposition
        self.calls = 0

    def chat(self, messages, tools=None, model=None, temperature=0.2, max_tokens=2048):
        self.calls += 1
        if self.calls % 2 == 1:
            return {
                "kind": "tool_calls",
                "tool_calls": [{
                    "id": f"c{self.calls}", "name": "query_candidate_evidence",
                    "arguments": json.dumps({
                        "device_sn": "device-abc123",
                        "component": "joint_motor.front_left_hip",
                        "window_start": "09:55", "window_end": "10:00",
                    }),
                    "content": "",
                }],
                "__raw_tool_calls": [],
            }
        return {
            "kind": "text",
            "content": json.dumps({
                "disposition": self.disposition,
                "severity": "warning", "confidence": 0.6,
                "summary": "左髋温度爬升超阈值",
                "hypotheses": [], "missing_evidence": [], "recommended_actions": [],
            }, ensure_ascii=False),
        }


def test_agent_auto_proposes_alert_for_review_report():
    # 用独立 outbox 隔离，避免污染全局
    box = ProposeOutbox()
    agent = AgentLoop(FakeProvider("needs_human_review"), InvestigationTools(EvidenceStore()),
                      propose_outbox=box)
    report = agent.investigate(_cand("c-hip", "joint_motor.front_left_hip"))
    rc = report.model_provenance.get("report_receipt")
    assert rc is not None, "needs_human_review 报告应自动提出上报意图"
    assert rc["ok"] is True and rc["receipt_id"].startswith("receipt_")
    assert report.disposition == "needs_human_review"


def test_no_propose_for_rejected():
    agent = AgentLoop(FakeProvider("rejected"), InvestigationTools(EvidenceStore()))
    report = agent.investigate(_cand("c-ok", "joint_motor.front_right_hip"))
    # 误报排除 → 不上报，报告上无 receipt
    assert "report_receipt" not in report.model_provenance
    print("PASS test_no_propose_for_rejected")
