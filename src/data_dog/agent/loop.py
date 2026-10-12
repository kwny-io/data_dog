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
from typing import Any

from ..core.redact import redact
from ..providers.client import (
    ChatProvider,
    LLMCallError,
    LLMChatMessage,
    LLMToolParam,
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
- **裁决口径（严格按这个顺序判，别把"没发现问题"当成"证据不足"）**：
  1. **rejected**：工具都成功返回、数据齐全，且观测值都在基线/阈值内、无异常模式。
     "设备正常"是一个**确定的结论**，就该判 rejected。
  2. **confirmed**：有明确证据支持异常（超阈值或显著偏离基线），且能指向具体原因。
  3. **inconclusive**：数据本身不完整或自相矛盾，无法形成任何判断。
  4. **needs_human_review**：**只在关键证据拿不到**（工具失败、时间窗缺数据、权限不足）
     或**冲突无法消解**时才用。
  ⚠️ 实测中最常见的错误是把"查完发现一切正常"判成 needs_human_review——那会把大量健康
  设备堆给值班人，是**误报**。**"没发现异常" ≠ "证据不足"**：前者要 rejected。

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


# 跨调查记忆（VISTA-1「无损记忆 + 主动检索」在工业诊断域的同构移植）。
# 只在证据源确实带既往报告时追加，避免提到一个并不存在的工具。
PRIOR_MEMORY_PROMPT = """

本部件有**历史调查记录**，可用 query_prior_investigations 调取（无损保留当时的裁决与
证据引用）。用法与边界：
- 它是**先验**，不是本次证据：**先查当前遥测/知识，再审既往**；不得因为"以前判过
  rejected"就跳过证据查询、直接下结论。
- 若当前证据与既往结论**相反**，以**当前**为准，并在 summary 里说明差异（与既往 X 不同）。
- 若本次关键证据拿不到，既往结论可降低不确定性，但**不能替代**缺失证据——该判
  inconclusive / needs_human_review 时照判。"""


# 视觉证据通道（VISTA-1 式）。只在挂了无损视觉记忆、且 provider 支持多模态时才有意义——
# 但"是否支持多模态"由调用方/部署决定，这里只按"工具是否可用"追加说明。
VISUAL_HARNESS_PROMPT = """

你还有**视觉证据通道**：候选窗口内设备相机的**原始帧**被无损留存（不重编码、不写成
文字描述）。用 query_visual_evidence 列出帧、view_visual_frame 调取某帧；要看局部细节就
带 box='l,t,r,b' 放大。原则（对齐 VISTA）：**需要细节时去调原帧再放大**，不要只凭文字
摘要或降采样印象下结论；但视觉证据也只是证据之一，仍须与遥测/知识交叉验证。"""


class AgentLoop:
    def __init__(
        self,
        provider: ChatProvider,
        tools: InvestigationTools,
        max_steps: int = 8,
        model: str | None = None,
        trace_cb=None,
        propose_outbox=None,
    ):
        self.provider = provider
        self.tools = tools
        self.max_steps = max_steps
        self.model = model
        self.trace_cb = trace_cb  # 每步工具调用的回调（供前端渲染 ReAct 轨迹）
        self.propose_outbox = propose_outbox  # A3：可注入本地 outbox（默认全局单例）
        self._current_tracker: Any = None   # 可观测性：正在进行的调查的用量累计器

    def _trace(self, event: dict) -> None:
        if self.trace_cb is not None:
            try:
                self.trace_cb(event)
            except Exception:
                pass  # 前端回调失败不影响调查主流程

    def investigate(self, candidate: CandidateEvent) -> InvestigationReport:
        # ---- 可观测性：本次调查的用量/耗时/成本累计器 ----
        from ..observability import UsageTracker
        tracker = UsageTracker()
        self._current_tracker = tracker

        # ---- A4：可信上下文（三选一，失败关闭）----
        # 1) DD_CTX_MCP=1 → 从 context-injector MCP 获取（拿不到=失败关闭）
        # 2) 候选设备与可信上下文设备不一致 → 失败关闭（不信任候选自报身份）
        # 3) 未启用 MCP → 按候选设备绑定（单机学习模式）
        from .. import settings
        trusted, ctx_err = None, None
        if settings.ctx_mcp() == "1":
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

        # 跨调查记忆说明只在工具可用时追加（否则系统提示会提到不存在的工具）。
        system_prompt = SYSTEM_PROMPT
        if any(s["name"] == "query_prior_investigations" for s in tool_specs):
            system_prompt += PRIOR_MEMORY_PROMPT
        if any(s["name"] == "view_visual_frame" for s in tool_specs):
            system_prompt += VISUAL_HARNESS_PROMPT

        messages: list[LLMChatMessage] = [
            LLMChatMessage(role="system", content=system_prompt),
            LLMChatMessage(
                role="user",
                content=(
                    f"请调查这个候选异常：\n"
                    f"{json.dumps(candidate.to_dict(), ensure_ascii=False, indent=2)}"
                ),
            ),
        ]

        steps = 0
        # 兼容性：有的 provider（测试 fake）不接受 usage_tracker 参数 → 检测签名
        _chat_accepts_usage = _accepts_kwarg(self.provider, "usage_tracker")
        while steps < self.max_steps:
            steps += 1
            try:
                if _chat_accepts_usage:
                    # usage_tracker 不在 ChatProvider 契约里：只有真 Provider 接受它，
                    # 确定性 provider 不接受。这里已用 _accepts_kwarg 做运行期探测，
                    # 是刻意的动态调用，故显式忽略类型检查。
                    resp = self.provider.chat(  # type: ignore[call-arg]
                        messages, tools=tool_params, model=self.model,
                        usage_tracker=tracker,
                    )
                else:
                    resp = self.provider.chat(
                        messages, tools=tool_params, model=self.model,
                    )
            except LLMCallError as e:
                # 模型调用失败（超时/不可用）→ 失败关闭，绝不假装查过证据。
                # 对齐 §9.4：Agent 不可用时普通候选交人工复核，不瞎判。
                return self._fail_closed(
                    candidate,
                    f"模型调用失败（{e.code.value}），失败关闭: {e.detail}")

            if resp["kind"] == "text":
                # 模型直接给结论（通常发生在它已经拿到足够证据时）
                report = self._parse_report(resp["content"], candidate)
                return self._finish_report(report)

            # 模型想调用工具 → 先执行所有工具、收集结果
            collected = []
            for tc in resp["tool_calls"]:
                name = tc["name"]
                try:
                    args = json.loads(tc["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = tools.call(name, args)
                # 视觉证据走独立的"图像消息"，不把 base64 塞进 tool 文本（会白烧 token）
                images = result.pop("__images__", None) if isinstance(result, dict) else None
                collected.append((tc["id"], result, images))
                # ReAct 轨迹：把这一步"想调的"记下来（此时 result 已摘掉 base64）
                self._trace({
                    "type": "tool_call",
                    "step": steps,
                    "name": name,
                    "args": args,
                    "result": result,
                })

            # 协议要求：一条 assistant 消息（回显全部 tool_calls）+
            # 每条 tool_call 对应一条 tool 消息
            messages.append(
                LLMChatMessage(
                    role="assistant",
                    content=resp.get("content") or "",
                    tool_calls=resp.get("__raw_tool_calls", []),
                )
            )
            for tc_id, result, images in collected:
                messages.append(
                    LLMChatMessage(
                        role="tool",
                        tool_call_id=tc_id,
                        content=json.dumps(result, ensure_ascii=False),
                    )
                )
                if images:
                    # VISTA-1 式：原始帧作为**独立观察**喂进上下文，供多模态模型直接看。
                    messages.append(
                        LLMChatMessage(
                            role="user",
                            content="（上一步检索到的视觉证据：设备相机原始帧）",
                            images=images,
                        )
                    )

        # 达到最大步数仍未给出结论
        report = InvestigationReport(
            investigation_id=f"inv_{candidate.candidate_id}",
            candidate_refs=[candidate.candidate_id],
            disposition="needs_human_review",
            confidence=0.0,
            summary="调查达到最大步数仍未得出确定结论，需要人工复核。",
            missing_evidence=["未能收集到足够证据"],
        )
        return self._finish_report(report)

    def _fail_closed(
        self, candidate: CandidateEvent, reason: str
    ) -> InvestigationReport:
        """失败关闭：拿不到可信上下文 / 身份不一致时，拒绝调查而不是降级。"""
        # 报告会被**持久化并经查询端点返回**，故这里再兜一层脱敏：reason 可能带
        # provider 异常详情（见 core/redact.py）。错误信息可以少，密钥不能漏。
        reason = redact(reason)
        return InvestigationReport(
            investigation_id=f"inv_{candidate.candidate_id}",
            disposition="needs_human_review",
            candidate_refs=[candidate.candidate_id],
            summary=f"身份/授权校验未通过，已失败关闭：{reason}",
            missing_evidence=[reason],
            model_provenance={"fail_closed": True},
        )

    def investigate_batch(
        self, candidates: list[CandidateEvent]
    ) -> tuple[list[InvestigationReport], list[dict]]:
        """一次调查多个候选：逐条复核 → 自动关联 → 把关联信息回填进报告。

        对应架构 6.2 里 Agent 的「复核候选 + 多事件关联 + 解释」：
        - 先逐条跑 investigate() 得到单候选报告；
        - 再用 Correlator 对整批候选做确定性关联（14.1 结构化关联键）；
        - 每个报告打上它所属的关联组（correlation_group_id）和同组关联到的
          其他候选（linked_candidate_refs），让上层的「关联+解释」能据此合并。

        返回 (reports, groups)；groups 是 CorrelationGroup.to_dict() 列表，供展示解释。
        """
        from ..correlation import Correlator

        reports = [self.investigate(c) for c in candidates]
        groups = Correlator().correlate(candidates)
        report_by_cid: dict[str, InvestigationReport] = {}
        for r, c in zip(reports, candidates, strict=False):
            report_by_cid.setdefault(c.candidate_id, r)

        # 把关联信息回填：一份报告知道它被分到哪个组、同组还有谁
        for g in groups:
            for cid in g.candidate_ids:
                hit = report_by_cid.get(cid)
                if hit is not None:
                    hit.correlation_group_id = g.group_id
                    hit.linked_candidate_refs = [
                        o for o in g.candidate_ids if o != cid
                    ]
        return reports, [g.to_dict() for g in groups]

    def _finish_report(
        self, report: InvestigationReport
    ) -> InvestigationReport:
        """调查收尾：附上 A3 上报 Receipt + 本次调查的可观测性统计（token/耗时/成本）。"""
        report = self._attach_report_receipt(report)
        # 可观测性：把本次调查的用量累计写进报告（供 CLI/前端展示）
        tracker = getattr(self, "_current_tracker", None)
        if tracker is not None:
            report.model_provenance["usage"] = tracker.summary()
        return report

    def _attach_report_receipt(
        self, report: InvestigationReport
    ) -> InvestigationReport:
        """A3：调查出结论后，把需要上报的报告"提出上报意图"并附上 Receipt。

        只对值得上报的裁决（confirmed / needs_human_review）触发；rejected（误报）
        不上报。Receipt 写进 model_provenance.report_receipt，可在报告里看到
        "这次调查想上报什么、拿到什么回执"，证明闭环走到"提出上报意图"这一步。
        """
        if report.disposition in ("confirmed", "needs_human_review"):
            from ..reporter import propose_alert_report
            inv_ref = report.investigation_id or ""
            rc = propose_alert_report(
                investigation_ref=inv_ref,
                summary=report.summary,
                severity=report.severity or "warning",
                outbox=self.propose_outbox,
            )
            if rc.get("ok"):
                report.model_provenance["report_receipt"] = rc
        return report

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


def _accepts_kwarg(provider, name: str) -> bool:
    """判断 provider.chat 是否接受某个关键字参数（用于兼容测试 fake / 真 Provider）。"""
    import inspect
    chat = getattr(provider, "chat", None)
    if chat is None:
        return False
    try:
        sig = inspect.signature(chat)
    except (TypeError, ValueError):
        return True  # 无法检查（如内置）就假定支持
    params = sig.parameters
    return name in params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
