# -*- coding: utf-8 -*-
"""data_dog —— MuJoCo 仿真数据源演示页（Streamlit）。

一个像"产品 demo"的网页：点按钮，自动：
  1. 用 MuJoCo 跑 Go2 仿真（受控小幅摆动）
  2. 展示 12 个关节的力矩/电流/温度（热模型算出）
  3. 喂给调查 Agent，6 段（正常+5故障）逐个裁决
  4. 汇总准确率

运行：
    python -m streamlit run ui/mujoco_page.py
    （收发端都走 mujoco_sim 的确定性 Provider，不需要 API Key，离线就能演示）
"""
from __future__ import annotations

import os
import sys

import streamlit as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

st.set_page_config(page_title="MuJoCo 仿真数据源演示", layout="wide",
                   page_icon="🤖")
st.title("🤖 MuJoCo 仿真数据源 —— Go2 遥测 → Agent 调查")


def run_and_capture(duration_s: float):
    """跑 mujoco_eval，返回样本级结果 + 遥测细节 + 汇总。"""
    from data_dog.mujoco_sim.simulate import simulate
    from data_dog.mujoco_sim.to_sample import to_synth_sample
    from data_dog.mujoco_sim.mujoco_eval import (
        build_mujoco_samples, MujocoDeterministicProvider,
        summarize_synth,
    )
    from data_dog.synth.evidence_source import SyntheticEvidenceSource
    from data_dog.agent.loop import AgentLoop
    from data_dog.tools.investigation import InvestigationTools

    faults = ["normal"] + ["F1_stall", "F2_temp_rise", "F3_packet_loss",
                           "F4_current_bias", "F5_load_spike"]
    sim = simulate(duration_s=duration_s)
    # 每个故障一段，注入到 front_left_knee
    results = []
    for i, f in enumerate(faults):
        comp = None if f == "normal" else "joint_motor.front_left_knee"
        s = to_synth_sample(sim, sample_id=f"mj_{f}_{i}",
                            active_fault=None if f == "normal" else f,
                            fault_component=comp or "joint_motor.front_left_knee")
        tel = s.telemetry("joint_motor.front_left_knee")
        source = SyntheticEvidenceSource([s])
        tools = InvestigationTools(source)
        prov = MujocoDeterministicProvider()
        agent = AgentLoop(prov, tools)
        from data_dog.schemas.events import CandidateEvent
        cand = CandidateEvent(
            candidate_id=f"cand_{i}", device_sn="device-go2",
            component_ref="joint_motor.front_left_knee",
            signal="synthetic_anomaly", severity_hint="warning",
            window_start="09:55", window_end="10:00", sample_count=300,
            rule_matches=["MJCF"], anomaly_score=0.5, detector_provenance="mujoco",
        )
        report = agent.investigate(cand)
        # ground truth 期望
        gt = ("rejected" if f == "normal" else "needs_human_review")
        hit = report.disposition == gt
        results.append({
            "fault": f,
            "gt": gt,
            "agent": report.disposition,
            "confidence": round(float(report.confidence), 2),
            "peak_temp": float(tel.get("peak_temperature_c", 0)),
            "current": round(float(tel["metrics"]["current_a"]), 1),
            "hit": hit,
        })
    summary = summarize_synth([{"hit": r["hit"]} for r in results])
    return sim, results, summary, tel


st.markdown("> 点击按钮，自动完成 **MuJoCo 跑仿真 → 出遥测 → Agent 调查 → 汇总准确率**。"
            "全程离线，用确定性 Provider 裁决（不依赖 API Key）。")

with st.sidebar:
    st.subheader("仿真参数")
    dur = st.slider("仿真时长（秒）", 10, 60, 30, step=5,
                    help="电机热容大，太短温度爬不起来；建议 ≥20s")
    run = st.button("🚀 开始演示", type="primary")

if run:
    sim, results, summary, tel = run_and_capture(dur)

    header = st.container()
    with header:
        c = st.columns(4)
        c[0].metric("仿真步数", f"{sim.ts.shape[0]}")
        c[1].metric("关节数", f"{len(sim.joints)}")
        c[2].metric("温度峰值", f"{sim.temperature.max():.0f} °C")
        c[3].metric("调查准确率", f"{summary['accuracy']:.0%}",
                    delta=f"{summary['hit']}/{summary['n']}")

    st.subheader("🌡️ 12 个关节的电机温度（热模型）")
    import numpy as np
    chart = st.container()
    with chart:
        st.bar_chart(sim.temperature.mean(axis=0),
                     x_label="关节", y_label="平均温度 °C")
    st.caption(f"初值全是 25°C 环境温度；膝盖(承重、电流大)明显发热，髋关节(不出力)基本不热"
               f"——每个关节按自己那份电流用一阶热模型独立升温，物理自洽。")

    st.subheader("🔬 Agent 逐段调查结果")
    rows = []
    for r in results:
        rows.append({
            "故障": r["fault"],
            "期望(gt)": r["gt"],
            "Agent 裁决": r["agent"],
            "置信度": r["confidence"],
            "峰值温度°C": r["peak_temp"],
            "电流A": r["current"],
            "命中": "✅" if r["hit"] else "❌",
        })
    st.dataframe(rows, use_container_width=True)

    st.info(f"**汇总结论**：{summary['n']} 段仿真（1 正常 + 5 类故障），Agent 命中 "
            f"{summary['hit']}，准确率 **{summary['accuracy']:.0%}**。"
            f"正常样本判「排除」，故障样本判「需复核」——分得清楚。")
else:
    st.caption("在左侧点「🚀 开始演示」运行。")
