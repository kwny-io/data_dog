"""跨调查记忆（A）：把 VISTA-1「无损记忆 + 主动检索」落到工业诊断的 Agent 上。

钉住三件事：
1. 证据源没有该能力时，工具**不出现在 specs**，且调用返回空 + 显式说明（不冒充）。
2. 有既往报告时该工具才出现，且过滤/排序/上限正确（无损保留当时的裁决与证据引用）。
3. AgentLoop 只在工具可用时把"先验不等于证据"的说明加进系统提示。

约定：每个 test 结尾 print("PASS <name>")。
"""
from __future__ import annotations

import json

from data_dog.agent.loop import PRIOR_MEMORY_PROMPT, AgentLoop
from data_dog.service.evidence import RepoEvidenceBridge
from data_dog.tools.investigation import InvestigationTools

COMP = "joint_motor.front_left_hip"


class _BareStore:
    """最小证据源：不提供跨调查记忆。"""

    def query_telemetry(self, *a, **k):
        return None

    def query_vendor_knowledge(self, *a, **k):
        return []

    def query_neighbor_events(self, *a, **k):
        return []

    def query_recent_changes(self, *a, **k):
        return {"changes": []}


def _prior(cid, created, component=COMP, signal="temperature_high",
           disposition="rejected"):
    return {"investigation_id": f"inv_{cid}", "candidate_id": cid,
            "component_ref": component, "signal": signal,
            "disposition": disposition, "severity": "info", "confidence": 0.8,
            "event_type": "none", "summary": f"prior {cid}",
            "created_at": created, "counter_evidence_refs": [f"ref://{cid}"]}


def test_tool_absent_without_capability():
    tools = InvestigationTools(_BareStore())
    names = {s["name"] for s in tools.specs()}
    assert "query_prior_investigations" not in names
    out = tools.call("query_prior_investigations", {"device_sn": "d1", "component": COMP})
    assert out["ok"] is True
    assert out["prior_investigations"] == []
    assert "未提供跨调查记忆" in out["note"]
    print("PASS test_tool_absent_without_capability")


def test_bridge_exposes_and_filters_priors():
    priors = [_prior("c1", "2026-01-01T00:00:00Z"),
              _prior("c2", "2026-03-01T00:00:00Z"),
              _prior("c3", "2026-02-01T00:00:00Z", component="joint_motor.rear_left_hip"),
              _prior("c4", "2026-04-01T00:00:00Z", signal="current_bias")]
    b = RepoEvidenceBridge([], device_sn="d1", prior_reports=priors)
    assert b.supports_prior_memory is True
    tools = InvestigationTools(b).with_task("d1")
    names = {s["name"] for s in tools.specs()}
    assert "query_prior_investigations" in names
    out = tools.call("query_prior_investigations", {"device_sn": "d1", "component": COMP})
    assert [p["candidate_id"] for p in out["prior_investigations"]] == ["c4", "c2", "c1"]
    out2 = tools.call("query_prior_investigations",
                      {"device_sn": "d1", "component": COMP, "signal": "current_bias"})
    assert [p["candidate_id"] for p in out2["prior_investigations"]] == ["c4"]
    print("PASS test_bridge_exposes_and_filters_priors")


def test_bridge_caps_at_five():
    priors = [_prior(f"c{i}", f"2026-01-{i:02d}T00:00:00Z") for i in range(1, 9)]
    b = RepoEvidenceBridge([], device_sn="d1", prior_reports=priors)
    out = InvestigationTools(b).with_task("d1").call(
        "query_prior_investigations", {"device_sn": "d1", "component": COMP})
    assert len(out["prior_investigations"]) == 5
    assert out["truncated"] is True
    assert out["prior_investigations"][0]["candidate_id"] == "c8"
    print("PASS test_bridge_caps_at_five")


def test_bridge_denies_out_of_scope_component():
    b = RepoEvidenceBridge([], device_sn="d1",
                           prior_reports=[_prior("c1", "2026-01-01T00:00:00Z")])
    out = InvestigationTools(b).with_task("d1").call(
        "query_prior_investigations", {"device_sn": "d1", "component": "joint_motor.not_a_real_part"})
    assert out["ok"] is False and out["error"] == "denied"
    print("PASS test_bridge_denies_out_of_scope_component")


class _RecordingProvider:
    """假 provider：记录系统提示，第一步直接给结论（不再调工具）。"""

    def __init__(self):
        self.system = ""

    def chat(self, messages, tools=None, model=None, temperature=0.2, max_tokens=2048):
        self.system = messages[0].content
        return {"kind": "text", "content": json.dumps(
            {"disposition": "rejected", "event_type": "none", "severity": "info",
             "confidence": 0.9, "summary": "正常", "hypotheses": [],
             "missing_evidence": [], "recommended_actions": []})}


def _candidate():
    from data_dog.schemas.events import CandidateEvent
    return CandidateEvent(candidate_id="cand_x", device_sn="d1", component_ref=COMP,
                          signal="temperature_high", severity_hint="warning",
                          window_start="09:55", window_end="10:00", sample_count=1)


def test_prompt_mentions_prior_only_when_available():
    prov = _RecordingProvider()
    AgentLoop(prov, InvestigationTools(_BareStore())).investigate(_candidate())
    assert PRIOR_MEMORY_PROMPT not in prov.system
    assert "query_prior_investigations" not in prov.system

    prov2 = _RecordingProvider()
    b = RepoEvidenceBridge([], device_sn="d1",
                           prior_reports=[_prior("c1", "2026-01-01T00:00:00Z")])
    AgentLoop(prov2, InvestigationTools(b)).investigate(_candidate())
    assert PRIOR_MEMORY_PROMPT in prov2.system
    print("PASS test_prompt_mentions_prior_only_when_available")
