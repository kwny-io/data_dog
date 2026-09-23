# -*- coding: utf-8 -*-
"""data_dog 调查演示台（Streamlit）。

布局（仪表盘式，尽量一眼看懂）：
    顶部品牌条（含状态徽章）
    → 一行指标卡（收到日志 / 兼容绑定 / 失败关闭 / 产出候选）
    → 一句话结论卡（大字号 + 人话解释）
    → 左右分栏：左＝场景与检测详情，右＝Agent 诊断与报告
    → 底部折叠：全部场景一览

流程对齐架构：**检测异常 → 诊断异常 → 待人工审核**。
候选由在线数据面（P2）从原始日志真实产出，不是手写清单；
Agent 查到的证据与候选来自同一批日志（单一事实源）。
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
    DEMO_DEVICE_SN, SCENARIOS, component_cn, match_scenarios, run_all,
)
from data_dog.e2e.bridge import CandidateEvidenceBridge  # noqa: E402
from data_dog.tools.investigation import InvestigationTools  # noqa: E402

st.set_page_config(page_title="data_dog · 机器狗遥测故障调查",
                   page_icon="🦾", layout="wide")

# ------------------------------------------------------------------ 样式
_CSS = """
<style>
  html, body, [data-testid="stAppViewContainer"] {
      font-family: -apple-system, "Segoe UI", "Microsoft YaHei", Roboto,
                   "Helvetica Neue", Arial, sans-serif;
  }
  .block-container { padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1560px; }
  [data-testid="stSidebar"] { background: #F8FAFC; border-right: 1px solid #E2E8F0; }

  /* 顶部品牌条 */
  .dd-hero { background: linear-gradient(120deg,#1E3A8A 0%,#2563EB 58%,#3B82F6 100%);
      border-radius: 14px; padding: 20px 26px; color: #fff;
      box-shadow: 0 10px 26px rgba(37,99,235,.20); margin-bottom: 18px; }
  .dd-hero .dd-h1 { font-size: 22px; font-weight: 700; letter-spacing: .2px; margin: 0; }
  .dd-hero .dd-sub { opacity: .92; font-size: 13px; margin-top: 5px; }
  .dd-hero .dd-tags { margin-top: 13px; display: flex; gap: 8px; flex-wrap: wrap; }
  .dd-tag { background: rgba(255,255,255,.16); border: 1px solid rgba(255,255,255,.30);
      border-radius: 999px; padding: 3px 11px; font-size: 12px; }

  /* 指标卡 */
  [data-testid="stMetric"] { background: #fff; border: 1px solid #E2E8F0;
      border-radius: 12px; padding: 13px 16px; box-shadow: 0 1px 6px rgba(15,23,42,.04); }
  [data-testid="stMetricLabel"] p { color: #64748B; font-size: 12.5px; }
  [data-testid="stMetricValue"] { font-size: 23px; font-weight: 700; color: #0F172A; }

  /* 结论卡 */
  .dd-verdict { border-radius: 14px; padding: 18px 22px; background: #fff;
      border: 1px solid #E2E8F0; border-left-width: 6px;
      box-shadow: 0 2px 12px rgba(15,23,42,.05); margin: 6px 0 4px 0; }
  .dd-verdict .dd-vt { font-size: 21px; font-weight: 700; margin: 0 0 6px 0; }
  .dd-verdict .dd-vs { color: #64748B; font-size: 14px; line-height: 1.6; }
  .dd-v-ok      { border-left-color: #059669; }
  .dd-v-warn    { border-left-color: #D97706; }
  .dd-v-danger  { border-left-color: #DC2626; }
  .dd-v-neutral { border-left-color: #64748B; }

  /* 小节标题 */
  .dd-sec { font-size: 13px; font-weight: 700; color: #64748B; letter-spacing: .6px;
      text-transform: uppercase; margin: 4px 0 8px 0; }

  /* 信息行 */
  .dd-kv { display: flex; gap: 10px; font-size: 14px; padding: 5px 0;
      border-bottom: 1px dashed #EDF1F7; }
  .dd-kv .k { color: #64748B; min-width: 72px; }
  .dd-kv .v { color: #0F172A; font-weight: 500; }

  /* 按钮圆角 */
  .stButton > button { border-radius: 10px; font-weight: 500; }
  [data-testid="stExpander"] { border-radius: 12px; }
</style>
"""
st.markdown(_CSS, unsafe_allow_html=True)


@st.cache_resource(show_spinner="正在跑在线数据面…")
def _load_runs():
    return {r.scenario.key: r for r in run_all()}


RUNS = _load_runs()
if "selected_key" not in st.session_state:
    st.session_state["selected_key"] = SCENARIOS[0].key


def _model_label() -> str:
    from data_dog.providers.config import load_config
    return "DeepSeek（真实模型）" if load_config().get("api_key") else "离线确定性模式"


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


def _verdict_class(run) -> str:
    if run.candidates:
        return "dd-v-danger" if run.candidates[0].severity_hint == "critical" else "dd-v-warn"
    return "dd-v-neutral" if (run.stats.get("rejected_by_reason") or {}) else "dd-v-ok"


def render_trace_step(step):
    icon = {"query_candidate_evidence": "🌡️", "query_vendor_knowledge": "📚",
            "query_neighbor_events": "🔗", "query_recent_changes": "🕐"}.get(
        step["name"], "🔧")
    with st.container(border=True):
        st.markdown(f"**第 {step['step']} 步**　{icon} `{step['name']}`")
        res = step.get("result", {})
        if isinstance(res, dict) and res.get("metrics"):
            st.markdown("↳ " + "　".join(f"`{k}={v}`" for k, v in res["metrics"].items()))
            base = res.get("reference_window_metrics")
            if base:
                st.caption("参照上一窗：" + "　".join(f"{k}={v}" for k, v in base.items()))
        else:
            st.caption("↳ " + json.dumps(res, ensure_ascii=False)[:220])


# ================================================================== 品牌条
st.markdown(f"""
<div class="dd-hero">
  <p class="dd-h1">🦾 data_dog · 机器狗遥测故障调查</p>
  <div class="dd-sub">检测异常 → 诊断异常 → 待人工审核　|　候选由在线数据面（P2）从原始日志真实产出</div>
  <div class="dd-tags">
    <span class="dd-tag">在线数据面 · P2</span>
    <span class="dd-tag">受控调查 Agent</span>
    <span class="dd-tag">模型：{_model_label()}</span>
    <span class="dd-tag">场景 {len(RUNS)} 个</span>
  </div>
</div>
""", unsafe_allow_html=True)

# ================================================================== 侧栏
with st.sidebar:
    st.markdown('<div class="dd-sec">选择日志场景</div>', unsafe_allow_html=True)
    query = st.text_input("按关键词筛选", placeholder="温度 / 固件 / 电流 / 正常",
                          label_visibility="collapsed")
    hits = match_scenarios(query)
    if not hits:
        st.warning("没有匹配的场景，换个词试试。")
    for s in hits:
        r_i = RUNS[s.key]
        picked = s.key == st.session_state["selected_key"]
        dot = "🔴" if r_i.candidates else ("🟠" if (r_i.stats.get("rejected_by_reason") or {})
                                           else "🟢")
        if st.button(("▸ " if picked else "") + f"{dot} {r_i.short_name}",
                     key=f"pick_{s.key}", width="stretch"):
            st.session_state["selected_key"] = s.key
            for k in ("trace", "report", "label", "cand"):
                st.session_state.pop(k, None)
            st.rerun()
    st.divider()
    st.caption("🔴 有异常　🟠 被拒绝　🟢 正常")

run = RUNS[st.session_state["selected_key"]]
s = run.stats

# ================================================================== 指标卡
c1, c2, c3, c4 = st.columns(4)
c1.metric("收到日志", s.get("ingested", 0))
c2.metric("兼容绑定", s.get("resolved", 0))
c3.metric("失败关闭", s.get("rejected", 0))
c4.metric("产出候选", s.get("candidates", 0))

# ================================================================== 结论卡
st.markdown(f"""
<div class="dd-verdict {_verdict_class(run)}">
  <p class="dd-vt">{run.verdict}</p>
  <div class="dd-vs">{run.verdict_detail}</div>
</div>
""", unsafe_allow_html=True)
st.write("")

# ================================================================== 左右分栏
left, right = st.columns([4, 6], gap="large")

with left:
    with st.container(border=True):
        st.markdown('<div class="dd-sec">场景信息</div>', unsafe_allow_html=True)
        st.markdown(f"""
<div class="dd-kv"><span class="k">场景</span><span class="v">{run.short_name}</span></div>
<div class="dd-kv"><span class="k">部件</span><span class="v">{component_cn(run.scenario.component)}</span></div>
<div class="dd-kv"><span class="k">设备</span><span class="v">{DEMO_DEVICE_SN}</span></div>
""", unsafe_allow_html=True)
        st.caption(run.scenario.note)

    with st.expander("原始日志（送进数据面的原文）"):
        st.json(run.scenario.events, expanded=True)
        if run.scenario.baseline_events:
            st.caption("参照窗（更早一批，用于判断趋势）")
            st.json(run.scenario.baseline_events, expanded=True)

    with st.expander("数据面处理链路"):
        st.caption("网关校验（身份注入 / 大小 / 编码 / 限流）→ 兼容绑定（拿不准就失败关闭）"
                   "→ 解析归一 → 检测 → 候选合并")
        reasons = s.get("rejected_by_reason") or {}
        if reasons:
            st.error("失败关闭原因：" + "、".join(f"{k} × {v}" for k, v in reasons.items())
                     + "　（拒绝而不是猜，属预期安全行为）")
        else:
            st.success("全部日志通过校验与兼容绑定。")

with right:
    with st.container(border=True):
        st.markdown('<div class="dd-sec">Agent 诊断</div>', unsafe_allow_html=True)
        if not run.candidates:
            st.info("这批日志没有产出候选，因此不进入诊断——原因见左侧「场景信息」与"
                    "「数据面处理链路」。这是数据面的判定结果，不是故障。")
        else:
            cand = run.candidates[0]
            sev_color = "red" if cand.severity_hint == "critical" else "orange"
            st.markdown(f"**候选异常**　{cand.component_ref}")
            b1, b2, b3 = st.columns(3)
            b1.badge(cand.severity_hint.upper(), color=sev_color)
            b2.badge(f"评分 {cand.anomaly_score}", color="gray")
            b3.badge(cand.candidate_id, color="blue")

            if st.button("▶ 开始调查", type="primary"):
                trace, report = run_investigation(run, cand)
                st.session_state.update(trace=trace, report=report,
                                        label=run.scenario.label, cand=cand)

            if st.session_state.get("trace"):
                st.markdown('<div class="dd-sec" style="margin-top:14px">调查过程</div>',
                            unsafe_allow_html=True)
                for step in st.session_state["trace"]:
                    render_trace_step(step)

    report = st.session_state.get("report")
    if report is not None:
        d = report.to_dict()
        disp = str(d.get("disposition", "?")).upper()
        badge_color = {"CONFIRMED": "red", "NEEDS_HUMAN_REVIEW": "orange",
                       "REJECTED": "green"}.get(disp, "gray")
        with st.container(border=True):
            st.markdown('<div class="dd-sec">调查结论</div>', unsafe_allow_html=True)
            x1, x2 = st.columns([3, 7])
            with x1:
                st.markdown(f"### {disp}")
                st.caption(f"置信度 {d.get('confidence', 0.0):.2f}")
                st.badge("需人工确认", color="orange", icon="⚠️")
            with x2:
                st.write(d.get("summary", ""))
            if d.get("hypotheses"):
                st.markdown("**可能原因**")
                for h in d["hypotheses"]:
                    st.markdown(f"- `{h.get('status')}` {h.get('cause')}")
            if d.get("missing_evidence"):
                st.markdown("**还缺的证据**　" + "、".join(d["missing_evidence"]))
            if d.get("recommended_actions"):
                st.markdown("**建议动作**")
                for a in d["recommended_actions"]:
                    st.markdown(f"- {a}")
            st.warning("Agent 给出的是**建议**：是否正式上报由策略门决定，最终由人审核。"
                       "系统不会自行宣称已处置。")

# ================================================================== 全部场景
with st.expander(f"全部场景一览（共 {len(RUNS)} 个：哪些报了、哪些没报、为什么）"):
    rows = [{"场景": r.short_name, "结论": r.verdict, "收到": r.stats.get("ingested", 0),
             "失败关闭": r.stats.get("rejected", 0), "候选": r.stats.get("candidates", 0),
             "说明": r.scenario.note.split("。")[0] + "。"} for r in RUNS.values()]
    st.dataframe(rows, width="stretch", hide_index=True)
    st.caption("「候选 0」不是故障：正常不误报、差一点不猜、厂商没写阈值就不编规则、"
               "固件不匹配就拒绝处理。")
