# -*- coding: utf-8 -*-
"""data_dog 调查演示台（Streamlit）。

展示三件事，模拟真实 Agent 工作流：
  1. 左侧：选择候选异常 + 「开始调查」
  2. 中间：ReAct 轨迹 —— Agent 每步「调用什么工具、传什么参、看到什么结果」实时显示
  3. 右侧：InvestigationReport 卡片（裁决/置信度/假设/缺失证据/建议）

运行：
    /d/.venv/Scripts/python.exe -m streamlit run ui/app.py
    （无 .env key 时用离线 FakeProvider 演示轨迹；有 key 走真实 DeepSeek）

说明：调查逻辑复用 data_dog 的 AgentLoop，只新增 trace 回调把中间步骤暴露给界面。
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
from data_dog.schemas.events import CandidateEvent  # noqa: E402

st.set_page_config(page_title="data_dog 调查台", layout="wide")
st.title("🦾 data_dog — 机器狗遥测故障调查 Agent")

CANDIDATES = {
    "左髋电机温度爬升（热退化剧本）": "evidence/candidate_temperature.json",
}


def get_provider():
    """有 .env key → 真实 DeepSeek；否则离线 FakeProvider。"""
    from data_dog.providers.config import load_config
    from data_dog.providers.client import Provider
    cfg = load_config()
    if cfg.get("api_key"):
        return Provider.from_config(cfg["vendor_id"], cfg["api_key"])
    return FakeProvider()


class FakeProvider:
    """离线演示用：模拟脚本化的 ReAct 轨迹。"""
    def __init__(self):
        self.n = 0

    def chat(self, messages, tools=None, model=None, **kw):
        self.n += 1
        if self.n <= 1:
            return {"kind": "tool_calls", "tool_calls": [{
                "id": "m1", "name": "query_candidate_evidence",
                "arguments": json.dumps({"device_sn": "device-abc123",
                                         "component": "joint_motor.front_left_hip",
                                         "window_start": "09:55", "window_end": "10:00"}),
                "content": ""}], "__raw_tool_calls": []}
        if self.n == 2:
            return {"kind": "tool_calls", "tool_calls": [{
                "id": "m2", "name": "query_recent_changes",
                "arguments": json.dumps({"device_sn": "device-abc123",
                                         "component": "joint_motor.front_left_hip",
                                         "since": "2026-08-10"}),
                "content": ""}], "__raw_tool_calls": []}
        return {"kind": "text", "content": json.dumps({
            "disposition": "needs_human_review", "severity": "warning", "confidence": 0.6,
            "summary": "温度78.2°C超过75°C阈值且电流上升，但固件3.2.1(8/12)变更冷却参数，需人工区分固件效应与真实退化。",
            "hypotheses": [{"cause": "固件3.2.1冷却变更导致", "status": "supported"},
                           {"cause": "轴承摩擦增大", "status": "supported"}],
            "missing_evidence": ["升级前基线"],
            "recommended_actions": ["复核固件基线", "检查轴承"],
        }, ensure_ascii=False)}


def run_investigation(path):
    """执行一次调查，返回 (trace, report)。"""
    trace = []
    evidence = EvidenceStore()
    tools = InvestigationTools(evidence)
    agent = AgentLoop(get_provider(), tools, trace_cb=trace.append)
    with open(path, encoding="utf-8") as f:
        cand = CandidateEvent(**json.load(f))
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
        c2.write("← " + json.dumps(res, ensure_ascii=False)[:400])


# ---------------- 点击按钮：立刻调查，一次完成 ----------------
if st.button("▶ 开始调查", type="primary"):
    path = os.path.join(ROOT, CANDIDATES["左髋电机温度爬升（热退化剧本）"])
    trace, report = run_investigation(path)
    st.session_state["trace"] = trace
    st.session_state["report"] = report

# ---------------- 渲染 ----------------
left, mid, right = st.columns([2, 4, 4])

with left:
    st.subheader("候选异常")
    st.selectbox("选择", list(CANDIDATES))
    st.caption("点击「开始调查」后，Agent 会查询遥测、近期变更等，逐步收敛结论。")

with mid:
    st.subheader("🧠 ReAct 轨迹")
    if st.session_state.get("trace"):
        for s in st.session_state["trace"]:
            render_trace_step(s)
    else:
        st.caption("开始调查后，这里会显示 Agent 每一步在做什么（调了哪个工具、传了什么参数、看到什么结果）。")

with right:
    st.subheader("📋 InvestigationReport")
    report = st.session_state.get("report")
    if report is not None:
        st.metric("裁决", report.disposition, help=f"置信度 {report.confidence}")
        st.markdown(report.summary)
        st.write("**假设:**")
        for h in report.hypotheses:
            st.write(f"- `{h.status}` {h.cause}")
        if report.missing_evidence:
            st.write("**缺失证据:**", "、".join(report.missing_evidence))
        if report.recommended_actions:
            st.write("**建议:**")
            for a in report.recommended_actions:
                st.write(f"- {a}")
    else:
        st.caption("调查完成后，这里显示结构化报告。")
