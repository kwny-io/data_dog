# -*- coding: utf-8 -*-
"""离线端到端测试：用假 Provider 跑通完整调查链路，不依赖网络/API。"""
import json

from data_dog.agent.loop import AgentLoop
from data_dog.evidence.store import EvidenceStore
from data_dog.schemas.events import CandidateEvent, EvidenceSummary
from data_dog.tools.investigation import InvestigationTools
from data_dog.errors import ToolErrorCode, err, ok


class FakeProvider:
    """模拟 LLM：第1步查遥测，第2步查厂商知识，第3步给出 JSON 结论。"""

    def __init__(self):
        self.calls = 0
        self.max_calls = 3

    def chat(self, messages, tools=None, model=None, temperature=0.2, max_tokens=2048):
        self.calls += 1
        if self.calls == 1:
            return {
                "kind": "tool_calls",
                "tool_calls": [{
                    "id": "c1",
                    "name": "query_candidate_evidence",
                    "arguments": json.dumps({
                        "device_sn": "device-abc123",
                        "component": "joint_motor.front_left_hip",
                        "window_start": "09:55",
                        "window_end": "10:00",
                    }),
                    "content": "",
                }],
                "__raw_tool_calls": [],
            }
        if self.calls == 2:
            return {
                "kind": "tool_calls",
                "tool_calls": [{
                    "id": "c2",
                    "name": "query_vendor_knowledge",
                    "arguments": json.dumps({"component_type": "joint_motor"}),
                    "content": "",
                }],
                "__raw_tool_calls": [],
            }
        return {
            "kind": "text",
            "content": json.dumps({
                "disposition": "confirmed",
                "event_type": "joint_motor_thermal_degradation",
                "severity": "warning",
                "confidence": 0.85,
                "summary": "温度持续偏高且电流高于基线，符合热退化。",
                "hypotheses": [
                    {"cause": "bearing_friction_or_cooling_degradation", "status": "supported"}
                ],
                "missing_evidence": ["ambient_temperature"],
                "recommended_actions": ["降低负载", "检查冷却"],
            }, ensure_ascii=False),
        }


def make_candidate() -> CandidateEvent:
    return CandidateEvent(
        candidate_id="cand_01JXYZ",
        device_sn="device-abc123",
        component_ref="joint_motor.front_left_hip",
        signal="temperature_rising_under_normalized_load",
        severity_hint="warning",
        window_start="09:55",
        window_end="10:00",
        sample_count=420,
        rule_matches=["MOTOR_TEMP_TREND_V2"],
        anomaly_score=0.91,
        evidence=[
            EvidenceSummary(
                key="temperature_c", from_value=61.0, to_value=78.2,
                baseline=60.0, unit="celsius",
            ),
            EvidenceSummary(
                key="current_a", from_value=6.1, to_value=9.7,
                baseline=6.2, unit="ampere",
            ),
        ],
    )


def test_agent_loop_end_to_end():
    fp = FakeProvider()
    tools = InvestigationTools(EvidenceStore())
    agent = AgentLoop(fp, tools)
    report = agent.investigate(make_candidate())

    assert fp.calls == 3, f"expected 3 steps, got {fp.calls}"
    assert report.disposition == "confirmed"
    assert report.event_type == "joint_motor_thermal_degradation"
    assert report.confidence == 0.85
    assert len(report.hypotheses) == 1
    assert report.hypotheses[0].cause == "bearing_friction_or_cooling_degradation"
    assert report.candidate_refs == ["cand_01JXYZ"]
    print("PASS test_agent_loop_end_to_end")


def test_evidence_query():
    from data_dog.evidence.store import EvidenceStore
    st = EvidenceStore()
    res = st.query_telemetry(
        "device-abc123", "joint_motor.front_left_hip", "09:55", "10:00"
    )
    assert res["metrics"]["temperature_c"] == 78.2
    # 越权部件应被拒绝
    denied = st.query_telemetry("device-abc123", "secret.other", "09:55", "10:00")
    assert denied["error"] == "denied"
    assert denied["ok"] is False
    print("PASS test_evidence_query")


def test_unified_error_semantics():
    # A5: 架构 13.4 要求至少区分这些错误码
    required = [
        "invalid_input", "denied", "not_found", "ambiguous",
        "version_conflict", "contract_mismatch", "timeout",
        "unavailable", "rate_limited", "execution_failed",
    ]
    codes = {c.value for c in ToolErrorCode}
    for r in required:
        assert r in codes, f"missing error code {r}"
    # err() 构造统一失败结构
    e = err(ToolErrorCode.NOT_FOUND, "detail-here")
    assert e["ok"] is False and e["error"] == "not_found" and e["detail"] == "detail-here"
    # ok() 构造统一成功结构
    s = ok({"metrics": 1})
    assert s["ok"] is True and s["metrics"] == 1
    print("PASS test_unified_error_semantics")


def test_all_tools_ok_err_wrapper():
    """A5 完整版：所有 4 个工具的成功/失败都走统一 ok()/err() 结构。"""
    t = InvestigationTools(EvidenceStore())
    # 成功路径 → ok:True
    r1 = t.query_candidate_evidence("dev", "joint_motor.front_left_hip", "09:55", "10:00")
    assert r1["ok"] is True and "metrics" in r1
    r2 = t.query_vendor_knowledge("joint_motor", "x")
    assert r2["ok"] is True and "items" in r2
    r3 = t.query_neighbor_events("dev", "joint_motor.front_left_hip", "09:55", "10:00")
    assert r3["ok"] is True and "events" in r3
    r4 = t.query_recent_changes("dev", "joint_motor.front_left_hip", "2026-08-10")
    assert r4["ok"] is True and "changes" in r4
    # 失败路径 → ok:False + 统一错误码
    denied = t.query_candidate_evidence("dev", "secret.other", "09:55", "10:00")
    assert denied["ok"] is False and denied["error"] == "denied"
    denied_changes = t.query_recent_changes("dev", "secret.other", "2026-08-10")
    assert denied_changes["ok"] is False and denied_changes["error"] == "denied"
    print("PASS test_all_tools_ok_err_wrapper")


def test_context_injection_a4():
    """A4：运行时可信身份注入。

    - call() 应把 device 强制为 ctx 的设备（忽略调用方传的，防跨设备）。
    - 授权范围外的 component → denied，不真正执行。
    """
    from data_dog.context import RuntimeContext
    # 一个只授权左髋电机、agent 是待验证的受限上下文
    ctx = RuntimeContext(
        tenant_id="t1",
        agent_id="vetted-agent",
        device_sn="device-abc123",
        allowed_components=frozenset({"joint_motor.front_left_hip"}),
    )
    t = InvestigationTools(EvidenceStore(), ctx)

    # 1) 组件在授权范围 → 成功，且 device 被注入为 ctx 设备（即使调用方传别的）
    r = t.call("query_candidate_evidence",
               {"device_sn": "device-ATTACKER", "component": "joint_motor.front_left_hip",
                "window_start": "09:55", "window_end": "10:00"})
    assert r["ok"] is True
    assert r["device_sn"] == "device-abc123"   # 注入覆盖了攻击者传的设备

    # 2) 组件超出授权范围 → denied，即使组件本身在全局白名单里
    denied = t.call("query_candidate_evidence",
                    {"device_sn": "device-abc123", "component": "battery.main",
                     "window_start": "09:55", "window_end": "10:00"})
    assert denied["ok"] is False and denied["error"] == "denied"

    # 3) 未知工具 → execution_failed；缺参数 → invalid_input
    assert t.call("no_such_tool", {})["error"] == "execution_failed"
    bad = t.call("query_candidate_evidence",
                 {"component": "joint_motor.front_left_hip"})  # 缺 window args
    assert bad["error"] == "invalid_input"
    print("PASS test_context_injection_a4")
