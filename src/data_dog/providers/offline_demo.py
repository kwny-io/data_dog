"""离线确定性 Provider：结论由**真实证据数据**推导，可离线跑、可测试。

用途：没有模型 key 时让演示/回归测试能跑完整调查链路（AgentLoop → 工具 → 报告），
且**不使用网络、不花 token、结果可复现**。

与"写死剧本"的区别：这里的判定读的是 `evidence.store` 里的**真实证据数据**
（近期变更表），而不是"看到部件名就返回某个结论"。所以证据一变，结论随之变。
"""
from __future__ import annotations

import json
from typing import Any

from ..core.injection import unwrap
from ..evidence.store import RECENT_CHANGES
from ..presentation import component_cn


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
            if not content:
                continue
            # 消息里的 JSON 可能是「不可信数据块」内的原文（A3 之后候选正文会被包起来）：
            # 先剥壳，再用 raw_decode 取**第一个** JSON 对象（块尾还有结束标签）。
            body = unwrap(content)
            idx = body.find("{")
            if idx < 0:
                continue
            try:
                obj = json.JSONDecoder().raw_decode(body[idx:])[0]
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
            self._conclusion(comp, str(self.cand.get("severity_hint", "info")),
                             str(self.cand.get("signal", ""))),
            ensure_ascii=False)}

    @staticmethod
    def _event_type(comp: str, signal: str) -> str:
        """从部件类型 + 信号确定性派生故障类型。

        prompt 契约要求结论含 `event_type`（如 joint_motor_thermal_degradation）；
        不产出会让 alerting 兜底成 `unknown_event`，令所有故障共用同一 event_type
        → `event_id`/`idempotency_key` 不再区分故障，跨故障可能撞同一条告警。
        """
        ctype = comp.split(".", 1)[0] or "component"
        sig = signal or "anomaly"
        return f"{ctype}_{sig}"

    @staticmethod
    def _conclusion(comp: str, severity: str, signal: str = "") -> dict[str, Any]:
        changes = RECENT_CHANGES.get(comp, [])
        event_type = DeterministicEvidenceProvider._event_type(comp, signal)
        # 摘要里放**人话部件名**。原来直接拼 `comp`（`joint_motor.front_left_hip`），
        # 于是告警详情页的摘要第一句是英文标识符——用户报的正是这个。
        # 认不出的部件 `component_cn` 原样返回，摘要仍然完整，只是那一截是英文。
        what_comp = component_cn(comp)
        if severity != "critical":
            return {
                "disposition": "rejected", "severity": "info", "confidence": 0.85,
                "event_type": event_type,
                "summary": f"{what_comp}未越过厂商保护阈值，判定为非异常。",
                "hypotheses": [], "missing_evidence": [],
                "recommended_actions": ["无需处理"],
            }
        if changes:
            what = changes[0].get("what", "近期变更")
            return {
                "disposition": "needs_human_review", "severity": "critical",
                "confidence": 0.6, "event_type": event_type,
                "summary": (f"{what_comp}温度越过厂商保护阈值，但该部件近期有变更（{what}），"
                            "无法区分变更效应与真实退化，需人工复核。"),
                "hypotheses": [{"cause": f"{what} 导致读数偏移", "status": "supported"},
                               {"cause": "真实热退化", "status": "supported"}],
                "missing_evidence": ["变更前同工况基线"],
                "recommended_actions": ["复核变更前后基线", "安排现场检查"],
            }
        return {
            "disposition": "confirmed", "severity": "critical", "confidence": 0.9,
            "event_type": event_type,
            "summary": (f"{what_comp}温度越过厂商保护阈值，且无变更干扰、证据完整，"
                        "判为明确热故障。"),
            "hypotheses": [{"cause": "热退化/冷却失效", "status": "supported"}],
            "missing_evidence": [],
            "recommended_actions": ["降低负载并安排检修", "检查冷却通道"],
        }
