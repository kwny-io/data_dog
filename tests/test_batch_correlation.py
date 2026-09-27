"""Agent 核心：一次调查多个候选 → 自动关联 → 合并解释（架构 6.2 的复核+关联+解释）。

用一个只查证据就返回结论的假 Provider，喂进多个候选：
- investigate_batch() 应逐条出报告；
- Correlator 应按结构化键（同设备+同窗+信号族）把它们分成关联组；
- 每份报告应回填 correlation_group_id / linked_candidate_refs；
- explain_episode() 把一组报告合成为一句解释性说明（不调 LLM）。
"""
import json

from data_dog.agent.loop import AgentLoop
from data_dog.correlation import explain_episode
from data_dog.evidence.store import EvidenceStore
from data_dog.schemas.events import CandidateEvent
from data_dog.tools.investigation import InvestigationTools


class FakeProvider:
    """一次查证据、一次给结论，便于逐条复核整批候选。"""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, model=None, temperature=0.2, max_tokens=2048):
        self.calls += 1
        if self.calls % 2 == 1:
            return {
                "kind": "tool_calls",
                "tool_calls": [{
                    "id": f"c{self.calls}",
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
        return {
            "kind": "text",
            "content": json.dumps({
                "disposition": "needs_human_review",
                "severity": "warning",
                "confidence": 0.6,
                "summary": "温度爬升超阈值，疑固件基线偏移，需人工复核。",
                "hypotheses": [{"cause": "固件冷却参数变更", "status": "supported"}],
                "missing_evidence": ["升级前基线"],
                "recommended_actions": ["复核固件基线"],
            }, ensure_ascii=False),
        }


def _cand(cid: str, component: str, signal: str) -> CandidateEvent:
    return CandidateEvent(
        candidate_id=cid,
        device_sn="device-abc123",
        component_ref=component,
        signal=signal,
        severity_hint="warning",
        window_start="09:55",
        window_end="10:00",
        sample_count=300,
        rule_matches=["TEST"],
        anomaly_score=0.5,
        detector_provenance="test-harness",
    )


def test_investigate_batch_correlates_and_explains():
    cands = [
        _cand("c-hip", "joint_motor.front_left_hip", "temperature_rising_under_normalized_load"),
        _cand("c-knee", "joint_motor.front_left_knee", "temperature_rising_under_normalized_load"),
        _cand("c-batt", "battery.main", "battery_state_check"),  # 不同信号族 → 另一组
    ]
    agent = AgentLoop(FakeProvider(), InvestigationTools(EvidenceStore()))
    reports, groups = agent.investigate_batch(cands)

    assert len(reports) == 3
    # 每个候选都出了报告
    assert {r.candidate_refs[0] for r in reports} == {"c-hip", "c-knee", "c-batt"}
    # 关联成组：thermal 的两条归一组，eletric/battery 自成一类
    assert len(groups) >= 2
    thermal = [g for g in groups if g["family"] == "thermal"][0]
    assert thermal["candidate_ids"] == ["c-hip", "c-knee"]

    # 回填：同组两条报告互相在 linked_candidate_refs
    by_id = {r.candidate_refs[0]: r for r in reports}
    hip, knee = by_id["c-hip"], by_id["c-knee"]
    assert hip.correlation_group_id == thermal["group_id"]
    assert knee.correlation_group_id == thermal["group_id"]
    assert hip.linked_candidate_refs == ["c-knee"]
    assert knee.linked_candidate_refs == ["c-hip"]
    # battery 组内唯一 → 无 linked 候选
    assert by_id["c-batt"].linked_candidate_refs == []

    # 解释：能把一组报告合成一句可读说明
    ep = explain_episode([hip, knee], thermal)
    assert "2" in ep and "thermal" in ep
    print("PASS test_investigate_batch_correlates_and_explains")


def test_explain_episode_verdict_rollup():
    """解释逻辑把组内多份报告的不同裁决合并成一个汇总说明。"""
    from data_dog.schemas.events import InvestigationReport

    r1 = InvestigationReport(
        investigation_id="i1", candidate_refs=["c1"],
        disposition="needs_human_review", confidence=0.6,
    )
    r2 = InvestigationReport(
        investigation_id="i2", candidate_refs=["c2"],
        disposition="rejected", confidence=0.8,
    )
    group = {"component_refs": ["joint_motor.front_left_hip", "joint_motor.front_left_knee"],
             "family": "thermal"}
    ep = explain_episode([r1, r2], group)
    assert "needs_human_review×1" in ep and "rejected×1" in ep
    assert "joint_motor" in ep
    print("PASS test_explain_episode_verdict_rollup")
