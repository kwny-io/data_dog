# -*- coding: utf-8 -*-
"""data_dog 调查演示台（Streamlit）—— 尽量让人一眼看懂。

页面就三步：**选场景 → 看结论 → 要不要深入**。
技术细节（原始日志、处理链路、全部场景明细）都收在折叠区里，不看也能懂。

  ① 选一个"日志场景"（这代表机器狗发生了什么）
  ② 看一句话结论：检测到异常 / 没检测到异常（附一句人话解释）
  ③ 有异常 → 点「开始调查」看 Agent 诊断过程与报告（报告需人工审核）

候选由在线数据面（P2）从原始日志真实产出，不是手写清单；
Agent 查到的证据与候选来自同一批日志。
"""
from __future__ import annotations

import json
import os
import sys

import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from data_dog.agent.loop import AgentLoop  # noqa: E402
from data_dog.data_plane.scenarios import (  # noqa: E402
    DEMO_DEVICE_SN, SCENARIOS, match_scenarios, run_all,
)
from data_dog.e2e.bridge import CandidateEvidenceBridge  # noqa: E402
from data_dog.tools.investigation import InvestigationTools  # noqa: E402

st.set_page_config(page_title="data_dog 调查台", layout="wide")
st.title("🦾 机器狗遥测故障调查")
st.caption("选一个日志场景 → 看有没有异常 → 要深究就点「开始调查」")


@st.cache_resource(show_spinner="正在跑在线数据面…")
def _load_runs():
    return {r.scenario.key: r for r in run_all()}


RUNS = _load_runs()
if "selected_key" not in st.session_state:
    st.session_state["selected_key"] = SCENARIOS[0].key


def run_investigation(run, cand):
    from data_dog.providers.config import load_config
    from data_dog.providers.client import Provider
    from data_dog.providers.offline_demo import DeterministicEvidenceProvider

    cfg = load_config()
    provider = (Provider.from_config(cfg["vendor_id"], cfg["api_key"])
                if cfg.get("api_key") else DeterministicEvidenceProvider())
    bridge = CandidateEvidenceBridge(run.telemetry_by_component,
                                     device_sn=DEMO_DEVICE_SN)
    trace = []
    agent = AgentLoop(provider, InvestigationTools(bridge), trace_cb=trace.append)
    return trace, agent.investigate(cand)


def render_trace_step(step):
    icon = {"query_candidate_evidence": "🌡️", "query_vendor_knowledge": "📚",
            "query_neighbor_events": "🔗", "query_recent_changes": "🕐"}.get(
        step["name"], "🔧")
    with st.container(border=True):
        st.markdown(f"**第{step['step']}步** {icon} 查了 `{step['name']}`")
        res = step.get("result", {})
        if isinstance(res, dict) and res.get("metrics"):
            st.write("　看到：" + " | ".join(f"{k}={v}" for k, v in res["metrics"].items()))
            base = res.get("reference_window_metrics")
            if base:
                st.caption("　（参照上一窗：" +
                           " | ".join(f"{k}={v}" for k, v in base.items()) + "）")
        else:
            st.caption("　" + json.dumps(res, ensure_ascii=False)[:200])


# ================================================================ 侧栏：选场景
with st.sidebar:
    st.subheader("选一个日志场景")
    query = st.text_input("按关键词筛选", placeholder="例如：温度 / 固件 / 电流")
    hits = match_scenarios(query)
    if not hits:
        st.warning("没有匹配的场景，换个词试试（温度 / 固件 / 电流 / 正常）。")
    for s in hits:
        run_i = RUNS[s.key]
        picked = s.key == st.session_state["selected_key"]
        # 按钮上带结论图标，选之前就知道结果
        mark = "⚠️" if run_i.candidates else "✅"
        if st.button(("▶ " if picked else "") + f"{mark} {run_i.short_name}",
                     key=f"pick_{s.key}", width="stretch"):
            st.session_state["selected_key"] = s.key
            for k in ("trace", "report", "label", "cand"):
                st.session_state.pop(k, None)
            st.rerun()

run = RUNS[st.session_state["selected_key"]]

# ================================================================ 结论区（最显眼）
st.markdown("---")
st.markdown(f"### 你选的场景：**{run.short_name}**")
st.markdown(f"##### {run.verdict}")
st.write(run.verdict_detail)

with st.expander("这一批日志长什么样（原始日志）"):
    st.json(run.scenario.events, expanded=True)
    if run.scenario.baseline_events:
        st.caption("参照窗（更早一批，用于判断趋势）：")
        st.json(run.scenario.baseline_events, expanded=True)

with st.expander("数据面是怎么处理的（技术细节）"):
    s = run.stats
    a, b, c, d = st.columns(4)
    a.metric("收到日志", s.get("ingested", 0))
    b.metric("兼容绑定", s.get("resolved", 0))
    c.metric("失败关闭", s.get("rejected", 0))
    d.metric("产出候选", s.get("candidates", 0))
    st.caption("处理顺序：网关校验 → 兼容绑定 → 解析归一 → 检测 → 候选合并")
    reasons = s.get("rejected_by_reason") or {}
    if reasons:
        st.error("被拒绝的原因：" + "、".join(f"{k} × {v}" for k, v in reasons.items()))
    st.markdown("**这个场景想说明什么：**")
    st.info(run.scenario.note)

# ================================================================ 诊断
st.markdown("---")
if not run.candidates:
    st.markdown("### 不进入诊断")
    st.write("数据面没有产出候选，所以没有可调查的对象。"
             "**这不是故障**——原因见上方「数据面是怎么处理的」。")
else:
    st.markdown("### 让 Agent 诊断这条异常")
    cand = run.candidates[0]
    st.caption(f"候选号 {cand.candidate_id} · 部件 {cand.component_ref} · "
               f"严重度 {cand.severity_hint} · 命中 {cand.rule_matches}")
    if st.button("▶ 开始调查", type="primary"):
        trace, report = run_investigation(run, cand)
        st.session_state["trace"] = trace
        st.session_state["report"] = report
        st.session_state["label"] = run.scenario.label
        st.session_state["cand"] = cand

    if st.session_state.get("trace"):
        st.markdown("#### 调查过程（Agent 每一步查了什么、看到什么）")
        for step in st.session_state["trace"]:
            render_trace_step(step)

    report = st.session_state.get("report")
    if report is not None:
        d = report.to_dict()
        st.markdown("---")
        st.markdown("### 调查结论")
        st.markdown(f"## {str(d.get('disposition', '?')).upper()}"
                    f"　（置信度 {d.get('confidence', 0.0):.2f}）")
        st.write(d.get("summary", ""))
        hyps = d.get("hypotheses", [])
        if hyps:
            st.write("**可能原因：**")
            for h in hyps:
                st.write(f"- {h.get('cause')}（{h.get('status')}）")
        if d.get("missing_evidence"):
            st.write("**还缺的证据：**", "、".join(d["missing_evidence"]))
        if d.get("recommended_actions"):
            st.write("**建议动作：**")
            for a in d["recommended_actions"]:
                st.write(f"- {a}")
        st.warning("**→ 需人工确认。** Agent 给的是建议；是否正式上报由策略门决定，"
                   "最终由人审核。系统不会自己宣称已处置。")

# ================================================================ 全部场景
with st.expander("全部场景一览（哪些报了、哪些没报、为什么）"):
    rows = [{"场景": r.short_name, "结论": r.verdict, "产出候选": r.stats.get("candidates", 0)}
            for r in RUNS.values()]
    st.dataframe(rows, width="stretch", hide_index=True)
    st.caption("「产出候选 0」不是故障：正常不误报、差一点不猜、"
               "厂商没写阈值就不编规则、固件不匹配就拒绝。")
