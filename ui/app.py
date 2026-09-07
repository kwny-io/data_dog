# -*- coding: utf-8 -*-
"""data_dog 调查演示台（Streamlit）。

展示三件事，模拟真实 Agent 工作流：
  1. 左侧：选候选异常 + 「开始调查」
  2. 中间：ReAct 轨迹 —— Agent 每步「调用什么工具、传什么参、看到什么结果」实时显示
  3. 右侧：InvestigationReport 卡片（裁决/置信度/假设/缺失证据/建议）

运行（先 pip install -e .，再）：
    python -m streamlit run ui/app.py
    （无 .env key 时用离线 FakeProvider 演示，不同候选得出不同结论；
      有 key 走真实 DeepSeek）

说明：调查逻辑复用 data_dog 的 AgentLoop，只新增 trace 回调把中间步骤暴露给界面。
候选来自 src/data_dog/candidates.py 的 TEST_CASES（正常/故障/电池 三案例）。
"""
from __future__ import annotations

import json
import os
import sys

import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from data_dog.agent.loop import AgentLoop  # noqa: E402
from data_dog.evidence.store import EvidenceStore  # noqa: E402
from data_dog.tools.investigation import InvestigationTools  # noqa: E402
from data_dog.candidates import TEST_CASES, make_candidate  # noqa: E402

st.set_page_config(page_title="data_dog 调查台", layout="wide")
st.title("🦾 data_dog — 机器狗遥测故障调查 Agent")

CANDIDATES = {c.label: c for c in TEST_CASES}


def get_provider():
    """有 .env key → 真实 DeepSeek；否则离线 FakeProvider（按候选部件出结论，保证对比）。"""
    from data_dog.providers.config import load_config
    from data_dog.providers.client import Provider
    cfg = load_config()
    if cfg.get("api_key"):
        return Provider.from_config(cfg["vendor_id"], cfg["api_key"])
    return FakeProvider()


class FakeProvider:
    """离线演示：按候选部件出剧本，保证不同候选 → 不同结论（有对比）。"""
    def __init__(self):
        self.n = 0
        self.cand = {}

    def _grab(self, messages):
        for m in messages:
            content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
            if content and "{" in content:
                try:
                    self.cand = json.loads(content[content.find("{"):])
                except Exception:
                    pass

    def chat(self, messages, tools=None, model=None, **kw):
        self._grab(messages)
        comp = self.cand.get("component_ref", "joint_motor.front_left_hip")
        ws = self.cand.get("window_start", "09:55")
        we = self.cand.get("window_end", "10:00")
        sn = self.cand.get("device_sn", "device-abc123")
        self.n += 1
        if self.n == 1:
            return {"kind": "tool_calls", "tool_calls": [{
                "id": "m1", "name": "query_candidate_evidence",
                "arguments": json.dumps({"device_sn": sn, "component": comp,
                                         "window_start": ws, "window_end": we}),
                "content": ""}], "__raw_tool_calls": []}
        if comp in ("joint_motor.front_right_hip", "battery.main"):
            return {"kind": "text", "content": json.dumps(self._conclusion(comp), ensure_ascii=False)}
        if self.n == 2:
            return {"kind": "tool_calls", "tool_calls": [{
                "id": "m2", "name": "query_recent_changes",
                "arguments": json.dumps({"device_sn": sn, "component": comp, "since": "2026-08-10"}),
                "content": ""}], "__raw_tool_calls": []}
        return {"kind": "text", "content": json.dumps(self._conclusion(comp), ensure_ascii=False)}

    @staticmethod
    def _conclusion(comp: str):
        if comp == "joint_motor.front_left_hip":
            return {
                "disposition": "needs_human_review", "severity": "warning", "confidence": 0.6,
                "summary": "温度78.2°C超过75°C阈值且电流上升，但固件3.2.1(8/12)变更冷却参数，需人工区分固件效应与真实退化。",
                "hypotheses": [{"cause": "固件3.2.1冷却变更导致", "status": "supported"},
                               {"cause": "轴承摩擦增大", "status": "supported"}],
                "missing_evidence": ["升级前基线"],
                "recommended_actions": ["复核固件基线", "检查轴承"],
            }
        if comp == "battery.main":
            return {
                "disposition": "rejected", "severity": "info", "confidence": 0.8,
                "summary": "BMS 检查：SOC 0.72 远高于低压告警阈 0.2，电压平台正常，判定非异常。",
                "hypotheses": [], "missing_evidence": [],
                "recommended_actions": ["继续例行巡检"],
            }
        return {
            "disposition": "rejected", "severity": "info", "confidence": 0.85,
            "summary": "温度61°C / 电流6.2A 均在正常基线内，未触发任何告警规则，判定误报排除。",
            "hypotheses": [], "missing_evidence": [],
            "recommended_actions": ["无需处理"],
        }


def run_investigation(case):
    """对某个候选案例执行一次调查，返回 (trace, report)。"""
    trace = []
    provider = get_provider()
    if isinstance(provider, FakeProvider):
        provider.n = 0
        provider.cand = {}
    cand = make_candidate(case)
    evidence = EvidenceStore()
    tools = InvestigationTools(evidence)
    agent = AgentLoop(provider, tools, trace_cb=trace.append)
    report = agent.investigate(cand)
    return trace, report


def render_trace_step(step):
    icon = {"query_candidate_evidence": "🌡️", "query_vendor_knowledge": "📚",
            "query_neighbor_events": "🔗", "query_recent_changes": "🕐"}.get(
        step["name"], "🔧")
    with st.container(border=True):
        c1, c2 = st.columns([3, 5])
        c1.markdown(f"**第{step['step']}步** {icon} `{step['name']}`")
        c1.caption("`" + json.dumps(step.get("args", {}), ensure_ascii=False)[:120] + "`")
        res = step.get("result", {})
        if isinstance(res, dict) and res.get("metrics"):
            m = res["metrics"]
            c2.write("← " + " | ".join(f"{k}={v}" for k, v in m.items()))
        else:
            c2.write("← " + json.dumps(res, ensure_ascii=False)[:400])


# ---------------- 交互：选中候选 → 点按钮调查，一次完成 ----------------
with st.sidebar:
    st.subheader("候选异常")
    label = st.selectbox("选择候选", list(CANDIDATES))
    case = CANDIDATES[label]
    st.caption(f"部件 `{case.component}` · 窗口 {case.window[0]}~{case.window[1]}")
    st.caption("点「开始调查」后，Agent 会查询遥测/厂商知识/近期变更，逐步收敛结论。")
    if st.button("▶ 开始调查", type="primary"):
        trace, report = run_investigation(case)
        st.session_state["trace"] = trace
        st.session_state["report"] = report
        st.session_state["label"] = label

left, mid, right = st.columns([2, 4, 4])

with left:
    st.subheader("本轮候选")
    if st.session_state.get("label"):
        st.markdown(f"**{st.session_state['label']}**")
    else:
        st.caption("选一个候选，点「开始调查」。")

with mid:
    st.subheader("🧠 ReAct 轨迹")
    if st.session_state.get("trace"):
        for s in st.session_state["trace"]:
            render_trace_step(s)
    else:
        st.caption("开始调查后，这里会显示 Agent 每一步在做什么（调了哪个工具、看到什么结果）。")

with right:
    st.subheader("📋 InvestigationReport")
    report = st.session_state.get("report")
    if report is not None:
        d = report.to_dict()
        disp = d.get("disposition", "?")
        st.markdown(f"### 裁决：**{disp.upper()}**")
        st.metric("置信度", f"{d.get('confidence', 0.0):.2f}")
        st.markdown(d.get("summary", ""))
        hyps = d.get("hypotheses", [])
        if hyps:
            st.write("**假设:**")
            for h in hyps:
                st.write(f"- `{h.get('status')}` {h.get('cause')}")
        if d.get("missing_evidence"):
            st.write("**缺失证据:**", "、".join(d["missing_evidence"]))
        for a in d.get("recommended_actions", []):
            st.write(f"- {a}")
        st.caption(f"来源：{st.session_state.get('label', '')}")
    else:
        st.caption("调查完成后，这里显示结构化报告。")
