# -*- coding: utf-8 -*-
"""AgentLoop：把「候选异常 → 受控 Tool → 证据 → 裁决」串成一次调查任务。

流程（对齐架构 13.1 Agent 调查流程）：
  1 读取候选异常摘要
  2 查询同部件时间窗证据
  3 查询厂商知识
  4 查询邻居部件事件（关联）
  5 建立与验证假设
  6 裁决 confirmed/rejected/inconclusive/needs_human_review
  7 输出结构化 InvestigationReport

这是「真 Agent」：每次决策都由 LLM 完成，通过函数式 Tool 提供证据，
最后要求模型输出严格 JSON 的调查报告。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from ..providers.client import (
    LLMChatMessage,
    LLMToolParam,
    Provider,
)
from ..schemas.events import (
    CandidateEvent,
    Hypothesis,
    InvestigationReport,
)
from ..tools.investigation import InvestigationTools

SYSTEM_PROMPT = """你是 data_dog 设备健康调查 Agent，负责复核机器人遥测产生的候选异常。

你的职责：
- 只依据受控 Tool 返回的证据下结论，绝不编造数据。
- 查询同部件时间窗、厂商知识、邻居事件、近期变更，建立有限数量的原因假设并寻找反证。
- 若证据不足以给出确定结论，应判为 inconclusive 或 needs_human_review，而不是强行确认。

你可以使用的工具（每次只调一个，逐步收集证据）：
- query_candidate_evidence: 查部件在时间窗内的遥测与参照
- query_vendor_knowledge: 查厂商知识条目（阈值/原因/安全限制）
- query_neighbor_events: 查同设备其他部件的异常（关联）
- query_recent_changes: 查部件近期固件/算子/配置变更（排查变更引入的异常）

收集到足够证据后，不再调用工具，而是输出一个 JSON 调查结论（用 Final Answer）：
{
  "disposition": "confirmed | rejected | inconclusive | needs_human_review",
  "event_type": "字符串故障类型，如 joint_motor_thermal_degradation",
  "severity": "warning | critical | info",
  "confidence": 0.0到1.0,
  "summary": "一句话结论，说明依据了什么证据",
  "hypotheses": [{"cause": "原因描述", "status": "supported|unsupported"}],
  "missing_evidence": ["未能获得的证据"],
  "risk_window": "建议检查时限，如 inspect within 24 hours，可省略",
  "recommended_actions": ["建议动作列表"]
}
只能输出这个 JSON，不要输出别的。"""


class AgentLoop:
    def __init__(
        self,
        provider: Provider,
        tools: InvestigationTools,
        max_steps: int = 8,
        model: str | None = None,
    ):
        self.provider = provider
        self.tools = tools
        self.max_steps = max_steps
        self.model = model

    def investigate(self, candidate: CandidateEvent) -> InvestigationReport:
        # ---- A4：可信上下文（三选一，失败关闭）----
        # 1) DD_CTX_MCP=1 → 从 context-injector MCP 获取（拿不到=失败关闭）
        # 2) 候选设备与可信上下文设备不一致 → 失败关闭（不信任候选自报身份）
        # 3) 未启用 MCP → 按候选设备绑定（单机学习模式）
        import os
        trusted, ctx_err = None, None
        if os.environ.get("DD_CTX_MCP") == "1":
            try:
                from ..mcp_context import fetch_trusted_context
                trusted = fetch_trusted_context()
            except Exception as e:  # MCP 配置了但失败 → 绝不降级为无鉴权
                ctx_err = f"可信上下文获取失败（MCP），失败关闭: {e}"

        if ctx_err:
            return self._fail_closed(candidate, ctx_err)

        if trusted is not None:
            if candidate.device_sn != trusted.device_sn:
                return self._fail_closed(
                    candidate,
                    f"候选设备 {candidate.device_sn} 与可信上下文设备 "
                    f"{trusted.device_sn} 不一致，失败关闭",
                )
            tools = self.tools.with_context(trusted)
        else:
            tools = self.tools.with_task(candidate.device_sn)

        tool_specs = tools.specs()
        tool_params = [LLMToolParam(**s) for s in tool_specs]

        messages: List[LLMChatMessage] = [
            LLMChatMessage(role="system", content=SYSTEM_PROMPT),
            LLMChatMessage(
                role="user",
                content=(
                    f"请调查这个候选异常：\n"
                    f"{json.dumps(candidate.to_dict(), ensure_ascii=False, indent=2)}"
                ),
            ),
        ]

        steps = 0
        while steps < self.max_steps:
            steps += 1
            resp = self.provider.chat(
                messages, tools=tool_params, model=self.model
            )

            if resp["kind"] == "text":
                # 模型直接给结论（通常发生在它已经拿到足够证据时）
                return self._parse_report(resp["content"], candidate)

            # 模型想调用工具 → 先执行所有工具、收集结果
            collected = []
            for tc in resp["tool_calls"]:
                name = tc["name"]
                try:
                    args = json.loads(tc["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = tools.call(name, args)
                collected.append((tc["id"], result))

            # 协议要求：一条 assistant 消息（回显全部 tool_calls）+
            # 每条 tool_call 对应一条 tool 消息
            messages.append(
                LLMChatMessage(
                    role="assistant",
                    content=resp.get("content") or "",
                    tool_calls=resp.get("__raw_tool_calls", []),
                )
            )
            for tc_id, result in collected:
                messages.append(
                    LLMChatMessage(
                        role="tool",
                        tool_call_id=tc_id,
                        content=json.dumps(result, ensure_ascii=False),
                    )
                )

        # 达到最大步数仍未给出结论
        return InvestigationReport(
            investigation_id=f"inv_{candidate.candidate_id}",
            candidate_refs=[candidate.candidate_id],
            disposition="needs_human_review",
            confidence=0.0,
            summary="调查达到最大步数仍未得出确定结论，需要人工复核。",
            missing_evidence=["未能收集到足够证据"],
        )

    def _fail_closed(
        self, candidate: CandidateEvent, reason: str
    ) -> InvestigationReport:
        """失败关闭：拿不到可信上下文 / 身份不一致时，拒绝调查而不是降级。"""
        return InvestigationReport(
            investigation_id=f"inv_{candidate.candidate_id}",
            disposition="needs_human_review",
            candidate_refs=[candidate.candidate_id],
            summary=f"身份/授权校验未通过，已失败关闭：{reason}",
            missing_evidence=[reason],
            model_provenance={"fail_closed": True},
        )

    def _parse_report(
        self, text: str, candidate: CandidateEvent
    ) -> InvestigationReport:
        cleaned = _extract_json(text)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # 模型没给合法 JSON → 做一次容错收尾
            data = {
                "disposition": "needs_human_review",
                "summary": "Agent 输出无法解析，需人工复核。原文: " + text[:200],
            }

        hypotheses = [
            Hypothesis(
                cause=h.get("cause", ""),
                status=h.get("status", "unverified"),
                evidence_refs=h.get("evidence_refs", []),
            )
            for h in data.get("hypotheses", [])
        ]
        return InvestigationReport(
            investigation_id=f"inv_{candidate.candidate_id}",
            candidate_refs=[candidate.candidate_id],
            disposition=data.get("disposition", "needs_human_review"),
            event_type=data.get("event_type"),
            severity=data.get("severity"),
            confidence=float(data.get("confidence", 0.0)),
            summary=data.get("summary", ""),
            hypotheses=hypotheses,
            missing_evidence=data.get("missing_evidence", []),
            risk_window=data.get("risk_window"),
            recommended_actions=data.get("recommended_actions", []),
        )


def _extract_json(text: str) -> str:
    """宽松地从模型输出里抽出第一个 { ... } 块。"""
    s = text.find("{")
    e = text.rfind("}")
    if s != -1 and e != -1 and e > s:
        return text[s : e + 1]
    return text
