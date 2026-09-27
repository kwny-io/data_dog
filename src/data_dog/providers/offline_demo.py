"""离线确定性 Provider：结论由**真实证据数据**推导，可离线跑、可测试。

用途：没有模型 key 时让演示/回归测试能跑完整调查链路（AgentLoop → 工具 → 报告），
且**不使用网络、不花 token、结果可复现**。

与"写死剧本"的区别：这里的判定读的是 `evidence.store` 里的**真实证据数据**
（近期变更表），而不是"看到部件名就返回某个结论"。所以证据一变，结论随之变。
"""
from __future__ import annotations

import json
from typing import Any

from ..evidence.store import RECENT_CHANGES


class DeterministicEvidenceProvider:
    """先查证据、再查变更，然后按证据判定（确定性）。

    判定规则：
      - 非 critical → rejected（未越过厂商保护阈值）
      - critical 且该部件有近期变更 → needs_human_review（无法区分变更效应与真实退化）
      - critical 且无变更干扰 → confirmed
    """

    def __init__(self, since: str = "2026-08-10"):
        self.n = 0
        self.cand: dict[str, Any] = {}
        self.since = since

    # ---- 从消息里取出候选（认 component_ref，避免把工具结果当成候选）----
    def _grab(self, messages: list[Any]) -> None:
        for m in messages:
            content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
            if not content or "{" not in content:
                continue
            try:
                obj = json.loads(content[content.find("{"):])
            except Exception:
                continue
            if isinstance(obj, dict) and "component_ref" in obj:
                self.cand = obj

    def chat(self, messages, tools=None, model=None, temperature=0.2,
             max_tokens=2048, **kw) -> dict[str, Any]:
        self._grab(messages)
        comp = self.cand.get("component_ref", "joint_motor.front_left_hip")
        ws = self.cand.get("window_start", "10:00")
        we = self.cand.get("window_end", "10:05")
        sn = self.cand.get("device_sn", "")
        self.n += 1

        if self.n == 1:
            return {"kind": "tool_calls", "tool_calls": [{
                "id": "m1", "name": "query_candidate_evidence",
                "arguments": json.dumps({"device_sn": sn, "component": comp,
                                         "window_start": ws, "window_end": we}),
                "content": ""}], "__raw_tool_calls": []}
        if self.n == 2:
            return {"kind": "tool_calls", "tool_calls": [{
                "id": "m2", "name": "query_recent_changes",
                "arguments": json.dumps({"device_sn": sn, "component": comp,
                                         "since": self.since}),
                "content": ""}], "__raw_tool_calls": []}
        return {"kind": "text", "content": json.dumps(
            self._conclusion(comp, str(self.cand.get("severity_hint", "info"))),
            ensure_ascii=False)}

    @staticmethod
    def _conclusion(comp: str, severity: str) -> dict[str, Any]:
        changes = RECENT_CHANGES.get(comp, [])
        if severity != "critical":
            return {
                "disposition": "rejected", "severity": "info", "confidence": 0.85,
                "summary": f"{comp} 未越过厂商保护阈值，判定为非异常。",
                "hypotheses": [], "missing_evidence": [],
                "recommended_actions": ["无需处理"],
            }
        if changes:
            what = changes[0].get("what", "近期变更")
            return {
                "disposition": "needs_human_review", "severity": "critical",
                "confidence": 0.6,
                "summary": (f"{comp} 温度越过厂商保护阈值，但该部件近期有变更（{what}），"
                            "无法区分变更效应与真实退化，需人工复核。"),
                "hypotheses": [{"cause": f"{what} 导致读数偏移", "status": "supported"},
                               {"cause": "真实热退化", "status": "supported"}],
                "missing_evidence": ["变更前同工况基线"],
                "recommended_actions": ["复核变更前后基线", "安排现场检查"],
            }
        return {
            "disposition": "confirmed", "severity": "critical", "confidence": 0.9,
            "summary": (f"{comp} 温度越过厂商保护阈值，且无变更干扰、证据完整，"
                        "判为明确热故障。"),
            "hypotheses": [{"cause": "热退化/冷却失效", "status": "supported"}],
            "missing_evidence": [],
            "recommended_actions": ["降低负载并安排检修", "检查冷却通道"],
        }
