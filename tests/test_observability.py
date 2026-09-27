"""可观测性：token 消耗 / 每步耗时 / 成本统计（后端采集 + 前端展示）。

验证：
- UsageTracker 正确累计 token / 耗时 / 次数 / 估算成本。
- 成本估算按供应商+模型价格表，未定价返回 0 且标记 cost_estimated。
- AgentLoop 调查完成后，把用量统计写进 report.model_provenance["usage"]。
"""
import json

from data_dog.agent.loop import AgentLoop
from data_dog.evidence.store import EvidenceStore
from data_dog.observability import (
    CallStat,
    UsageTracker,
    fmt_cost,
    lookup_price,
)
from data_dog.schemas.events import CandidateEvent
from data_dog.tools.investigation import InvestigationTools


def test_usage_cost_estimation():
    t = UsageTracker()
    t.record(CallStat(model="deepseek-chat", vendor_id="deepseek",
                      prompt_tokens=1000, completion_tokens=2000,
                      duration_ms=500.0))
    # deepseek-chat 价格: in 0.27 / out 1.10 每1M
    cost = t.total_cost_usd
    expected = (1000 / 1e6) * 0.27 + (2000 / 1e6) * 1.10
    assert abs(cost - expected) < 1e-9
    assert t.total_tokens == 3000
    assert t.call_count == 1
    assert t.total_duration_ms == 500.0
    s = t.summary()
    assert s["total_tokens"] == 3000
    assert s["cost_estimated"] is False
    assert isinstance(fmt_cost(cost), str)
    print("PASS test_usage_cost_estimation")


def test_unpriced_model_marks_estimated():
    t = UsageTracker()
    t.record(CallStat(model="unknown-model", vendor_id="foo",
                      prompt_tokens=10, completion_tokens=10))
    assert t.total_cost_usd == 0.0
    assert t.summary()["cost_estimated"] is True
    assert lookup_price("foo", "unknown-model") is None
    print("PASS test_unpriced_model_marks_estimated")


def _cand(cid: str = "c-usage") -> CandidateEvent:
    return CandidateEvent(
        candidate_id=cid, device_sn="device-abc123",
        component_ref="joint_motor.front_left_hip",
        signal="temperature_rising_under_normalized_load",
        severity_hint="warning",
        window_start="09:55", window_end="10:00", sample_count=300,
        rule_matches=["T"], anomaly_score=0.9, detector_provenance="test",
    )


class UsageRecordingProvider:
    """模拟真 Provider：每次 chat 把用量记录进 UsageTracker（等价于 real 路径）。"""

    def __init__(self):
        self.tracker = UsageTracker()
        self.n = 0

    def chat(self, messages, tools=None, model=None,
             temperature=0.2, max_tokens=2048, usage_tracker=None):
        model = model or "deepseek-chat"   # 真 Provider 会用 vendor.default_model 兜底
        # 真 Provider 会把 usage_tracker 传进来并 record；这里显式记录
        target = usage_tracker or self.tracker
        target.record(CallStat(
            model=model, vendor_id="deepseek",
            prompt_tokens=900, completion_tokens=self.n + 1,
            duration_ms=120.0, kind="tool_calls" if self.n == 0 else "text",
        ))
        self.n += 1
        if self.n == 1:
            return {"kind": "tool_calls", "tool_calls": [{
                "id": "u1", "name": "query_candidate_evidence",
                "arguments": json.dumps({
                    "device_sn": "device-abc123",
                    "component": "joint_motor.front_left_hip",
                    "window_start": "09:55", "window_end": "10:00",
                }),
                "content": ""}], "__raw_tool_calls": []}
        return {"kind": "text", "content": json.dumps({
            "disposition": "needs_human_review", "confidence": 0.6,
            "summary": "热退化需人工。", "hypotheses": [],
            "missing_evidence": [], "recommended_actions": [],
        }, ensure_ascii=False)}


def test_agent_attaches_usage_to_report():
    agent = AgentLoop(UsageRecordingProvider(), InvestigationTools(EvidenceStore()))
    report = agent.investigate(_cand())
    usage = report.model_provenance.get("usage")
    assert usage is not None, "调查完成后报告应带上 usage 统计"
    assert usage["call_count"] == 2          # 一次查证据 + 一次结论
    assert usage["total_tokens"] > 0
    assert usage["duration_ms"] > 0
    print(f"PASS test_agent_attaches_usage_to_report (tokens={usage['total_tokens']}, "
          f"cost=${usage['cost_usd']})")
