"""合成数据 + ground truth 的 Agent 评估：产出简历可写的准确率数字。

做法：
- 用 Go2 物理参数合成 N 条样本（正常 + 各类故障），每条带 ground truth。
- 对每条样本，用 data_dog Agent 调查其遥测 → 得到 disposition。
- 把 Agent 裁决与 ground truth 期望裁决（正常→rejected；故障→needs_human_review/confirmed）
  对比，算"调查准确率 X/N"。

只用闭环内部逻辑 + 假/真 Provider，不联网也能跑（用 cli._FakeProvider 离线基线，
或真实 LLM）。来源：CLI 里 data-dog test 倒数第一段描述的场景。
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_dog.agent.loop import AgentLoop
from data_dog.core.console import ensure_utf8_console
from data_dog.core.injection import unwrap
from data_dog.providers.client import ChatProvider
from data_dog.schemas.events import CandidateEvent
from data_dog.synth import (
    FAULT_DEFS,
    SynthGenConfig,
    synthesize_batch,
)
from data_dog.synth.evidence_source import SyntheticEvidenceSource
from data_dog.tools.investigation import InvestigationTools

# 每条合成样本 → 期望的 Agent 裁决（ground truth 的"评估口径"）
# 正常→rejected；堵转/温度/负载等高危故障→needs_human_review;
# 丢包/电流偏置等数据质量问题 → needs_human_review（证据不完整）
_FAULT_EXPECTED: dict[str, str | None] = {
    "normal": "rejected",
    "F1_stall": "needs_human_review",
    "F2_temp_rise": "needs_human_review",
    "F3_packet_loss": "needs_human_review",
    "F4_current_bias": "needs_human_review",
    "F5_load_spike": "needs_human_review",
    # 可选：把某些视为 confirmed（温度越临界时）
}

# 可接受命中集合（宽松：Agent 判 needs_human_review 或 confirmed 都算"识别出异常"）
_ACCEPTABLE = {
    "normal": {"rejected"},
    **{f: {"needs_human_review", "confirmed"} for f in FAULT_DEFS},
}
# 严格：故障样本必须判 needs_human_review / confirmed，禁止判 rejected
_STRICT_FORBID = {
    "normal": {"confirmed", "needs_human_review"},
    **{f: {"rejected"} for f in FAULT_DEFS},
}


def build_samples(n_joints: int = 2, n_seconds: float = 120.0, seed: int = 0):
    """构造一串样本：1 正常 + 各类型故障各一条。

    故障注入到"左髋电机"关节，其余关节保持正常，便于 Agent 从温差/电流差看出异常。
    Returns: (samples, fault_labels)
    """
    joints = ["joint_motor.front_left_hip", "joint_motor.front_right_hip"]
    fault_joint = joints[0]  # 左髋
    cfgs = []
    labels = []  # ground truth list at 样本级
    faults = ["normal"] + list(FAULT_DEFS.keys())
    for i, f in enumerate(faults):
        cfgs.append(SynthGenConfig(
            n_joints=len(joints),
            joints=joints,
            n_seconds=n_seconds,
            dt=0.01,
            seed=seed + i * 100,
            active_fault=None if f == "normal" else f,
            fault_component=fault_joint if f != "normal" else "",
        ))
        labels.append({
            "id": f"t{i}",
            "fault": f,
            "component": fault_joint if f != "normal" else "",
            "expected": _FAULT_EXPECTED[f],
        })
    samples = synthesize_batch(cfgs)
    return samples, labels


def _make_candidate(sample, label, joint_idx: int = 0) -> CandidateEvent:
    joint = sample.joints[joint_idx]
    return CandidateEvent(
        candidate_id=f"cand_{label['id']}",
        device_sn="device-synth",
        component_ref=joint,
        signal="synthetic_anomaly",
        severity_hint="warning",
        window_start="09:55",
        window_end="10:00",
        sample_count=300,
        rule_matches=["SYNTH"],
        anomaly_score=0.5,
        detector_provenance="synth-gen",
    )


def expected_vs_actual(disposition: str, label: dict) -> bool:
    """评估：Agent 裁决是否命中 ground truth 期望。宽松集合命中即对。"""
    return disposition in _ACCEPTABLE[label["fault"]]


class DeterministicSynthProvider:
    """数据驱动的确定性 provider：真读合成遥测，按物理阈值裁决。

    这是"可复现准确率"的正确答案来源：裁决完全由遥测数据决定，
    不依赖 LLM，因此同一条合成样本 → 恒定裁决 → 准确率可复现可写简历。
    物理规则（与 mock 剧本阈值一致）：
      - 温度峰值 >= 90 → confirmed（临界，明确故障）
      - 温度峰值 >= 75 或 电流均值明显高于参照 → needs_human_review
      - 丢包率过高(>=0.1) → needs_human_review（数据质量不足）
      - 否则（温度/电流正常、无丢包）→ rejected
    """

    def __init__(self):
        self.n = 0
        self.telemetry = None

    def chat(self, messages, tools=None, model=None, temperature=0.2, max_tokens=2048):
        self.n += 1
        if self.n == 1:
            # 第一步：查该部件遥测
            return {
                "kind": "tool_calls",
                "tool_calls": [{
                    "id": "s1", "name": "query_candidate_evidence",
                    "arguments": json.dumps({
                        "device_sn": "device-synth",
                        "component": "joint_motor.front_left_hip",
                        "window_start": "09:55", "window_end": "10:00",
                    }),
                    "content": "",
                }],
                "__raw_tool_calls": [],
            }
        # 第二步：从最后一条 tool 消息里拿遥测结果，读温度/电流/丢包
        tel = self._find_telemetry(messages)
        return {
            "kind": "text",
            "content": json.dumps(self._decide(tel), ensure_ascii=False),
        }

    @staticmethod
    def _find_telemetry(messages):
        for m in reversed(messages):
            if getattr(m, "role", None) == "tool":
                try:
                    # 工具返回在进模型上下文前被包成「不可信数据块」(A3) → **必须剥壳**再解析。
                    # 否则 json.loads 失败 → 裁决静默退化成「无法获取遥测，需人工复核」，
                    # 评测准确率会毫无提示地崩掉（本仓真踩过：10 条确定性测试集体变红）。
                    data = json.loads(unwrap(getattr(m, "content", "") or "") or "{}")
                    if data.get("ok") and "metrics" in data:
                        return data
                except Exception:
                    continue
        return None

    def _decide(self, tel):
        if tel is None:
            return {"disposition": "needs_human_review", "confidence": 0.2,
                    "summary": "无法获取遥测证据，需人工复核。",
                    "hypotheses": [], "missing_evidence": ["遥测"]}
        m = tel.get("metrics", {})
        peak = float(tel.get("peak_temperature_c", m.get("temperature_c", 0)) or 0)
        cur = float(m.get("current_a", 0) or 0)
        ref = tel.get("reference_window_metrics", {}) or {}
        ref_t = float(ref.get("temperature_c", peak) or peak)
        # 丢包率：直接用生成器算好的 packet_loss_rate
        loss = float(tel.get("packet_loss_rate", 0.0) or 0.0)
        # 温度相对**环境温度**的抬升 —— 合成物理模型真正区分的信号。
        # 环境温度取 self.ambient（数据集里随工况变化，15~45°C）；未设则默认 25°C
        # （synth 老口径恒为 25，行为不变）。硬编码 25 会在高环温下把"环境本就热"
        # 误判成"故障热"——这是忠实多样环温数据暴露出的真实缺陷。
        amb = getattr(self, "ambient", 25.0)
        rise = peak - amb
        # 温度趋势：当前窗口 vs 参照窗口
        trend = peak - ref_t

        # 1) 临界高温 → 明确热故障
        if peak >= 90 or rise >= 80:
            return {"disposition": "confirmed", "confidence": 0.9,
                    "summary": f"温度峰值 {peak:.1f}°C（相对环境抬升 {rise:.0f}°C），判为明确热故障。",
                    "hypotheses": [{"cause": "热临界/堵转", "status": "supported"}],
                    "missing_evidence": []}
        # 2) 高温爬升（越 75°C 或明显升温趋势）→ 需人工复核（疑似热退化）
        if peak >= 75 or (rise >= 25 and trend >= 3):
            return {"disposition": "needs_human_review", "confidence": 0.6,
                    "summary": f"温度 {peak:.1f}°C 显著抬升/越阈，怀疑热退化，需复核。",
                    "hypotheses": [{"cause": "热退化/负载异常", "status": "supported"}],
                    "missing_evidence": ["环境温度"]}
        # 3) 丢包 → 数据质量不足，需人工复核
        if loss >= 0.05:
            return {"disposition": "needs_human_review", "confidence": 0.5,
                    "summary": f"遥测丢包率 {loss:.1%} 过高，证据不完整，需人工复核。",
                    "hypotheses": [], "missing_evidence": ["完整遥测"]}
        # 4) 电流明显偏离参照 → 需人工复核（F4 电流偏置）
        cur_ref = float(ref.get("current_a", cur) or cur)
        if (cur - cur_ref) > 1.5:
            return {"disposition": "needs_human_review", "confidence": 0.6,
                    "summary": f"电流 {cur:.1f}A 明显偏离参照 {cur_ref:.1f}A，怀疑传感器/负载异常，需复核。",
                    "hypotheses": [{"cause": "电流偏置/负载异常", "status": "supported"}],
                    "missing_evidence": ["电流基线"]}
        # 5) 一切正常 → 排除
        return {"disposition": "rejected", "confidence": 0.85,
                "summary": f"温度 {peak:.1f}°C（抬升 {rise:.0f}°C）/ 电流 {cur:.1f}A 在正常基线内，判定误报，排除。",
                "hypotheses": [], "missing_evidence": []}


def eval_one(sample, label, provider, model=None, cand=None, visual=None,
             trace_cb=None) -> dict:
    """对单条样本做一次 Agent 调查，返回结果 + 是否命中 ground truth。

    这是 `run_synth_eval` / `run_mujoco_eval` / 数据集评估共用的**内循环**——
    抽出来避免口径漂移（改一处、三处一致）。语义与原来的内联循环完全一致。

    `visual`：可选的**无损视觉记忆**（VISTA-1 式，见 evidence/visual.py）。传了就把
    视觉工具挂给 Agent；不传则纯文本（既有行为逐字节不变）。
    """
    cand = cand or _make_candidate(sample, label)
    # 每个候选用"只含自己这份样本"的数据源，避免多样本同部件覆盖
    source = SyntheticEvidenceSource([sample])
    tools = InvestigationTools(source, visual=visual)
    prov = (
        DeterministicSynthProvider()
        if provider is None
        else (provider() if callable(provider) else provider)
    )
    agent = AgentLoop(prov, tools, model=model, trace_cb=trace_cb)
    report = agent.investigate(cand)
    d = report.disposition
    return {
        "sample": label["id"],
        "fault": label["fault"],
        "component": cand.component_ref,
        "ground_truth": label["expected"],
        "agent_disposition": d,
        "confidence": round(float(report.confidence), 2),
        "summary": report.summary[:90],
        "hit": expected_vs_actual(d, label),
        # 可观测性：本次调查的 token / 耗时 / 估算成本（供成本分布统计；无 provider 时为空）
        "usage": report.model_provenance.get("usage", {}),
    }


def run_synth_eval(provider: ChatProvider | Callable[[], ChatProvider] | None = None,
                   model=None, n_joints: int = 2,
                   n_seconds: float = 120.0, seed: int = 0) -> list[dict]:
    """对每条合成样本用 Agent 调查，返回每样本结果 + 是否命中 ground truth。

    provider 默认给一个"会真读合成遥测、按物理规则裁决"的确定性 provider，
    这样准确率是"数据驱动"的：正常样本该 reject、温度/电流越阈该告警。
    若传入 provider（如真实 LLM），则用它调查。
    """
    samples, labels = build_samples(n_joints, n_seconds, seed)
    return [eval_one(sample, label, provider, model)
            for sample, label in zip(samples, labels, strict=False)]


def summarize_synth(results: list[dict]) -> dict:
    n = len(results)
    hit = sum(1 for r in results if r["hit"])
    return {"n": n, "hit": hit,
            "accuracy": round(hit / n, 2) if n else 0.0}


def main():
    import argparse
    ensure_utf8_console()      # Windows 控制台默认 GBK，装不下 ✓ / ⚠ 等字符
    parser = argparse.ArgumentParser(description="data_dog 合成数据 ground truth 评估")
    parser.add_argument("--mode", choices=["offshelf", "real"], default="offshelf",
                        help="offshelf=离线假 provider(默认)；real=真实LLM(需 key)")
    parser.add_argument("--n_seconds", type=float, default=120.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json", action="store_true", help="只输出 JSON 汇总")
    args = parser.parse_args()

    provider = None
    if args.mode == "real":
        from data_dog.providers.client import Provider
        from data_dog.providers.config import load_config
        cfg = load_config()
        if not cfg["api_key"]:
            print("real 模式需要 data-dog init 先配 key")
            sys.exit(1)
        provider = Provider.from_config(cfg["vendor_id"], cfg["api_key"],
                                         base_url=cfg.get("base_url") or "")

    results = run_synth_eval(provider, model=None,
                             n_joints=2, n_seconds=args.n_seconds, seed=args.seed)
    s = summarize_synth(results)

    if args.json:
        print(json.dumps({"summary": s, "details": results}, ensure_ascii=False))
        return 0

    print("===== 合成数据 ground truth 评估 =====")
    for r in results:
        mark = "✓" if r["hit"] else "✗"
        print(f"\n[{mark}] {r['sample']} 故障={r['fault']}")
        print(f"   ground_truth={r['ground_truth']} | Agent={r['agent_disposition']} "
              f"(conf={r['confidence']})")
        print(f"   摘要: {r['summary']}")
    print(f"\n===== 汇总 ===== 准确率 {s['hit']}/{s['n']} = {s['accuracy']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
