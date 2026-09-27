"""MuJoCo 数据源下的 Agent 评估：把 Go2 仿真遥测喂给调查 Agent，算准确率。

与 `synth/synth_eval.py` 完全同构，唯一区别是"样本从哪来"：
- synth_eval  用纯 NumPy 合成生成器（无 MuJoCo）
- 本模块       用 MuJoCo 跑 Go2 得到物理自洽的关节遥测（simulate）→ 转 SynthSample
              （to_synth_sample）→ 同一个 Agent 调查。

复用 synth_eval 里的确定性 Provider、评估函数、ground truth 期望表，
保证"接 mujoco 只是换数据源、Agent/评估逻辑不动"的架构边界成立。

用法：
    python -m data_dog.mujoco_sim.mujoco_eval            # 离线，确定性 provider
    python -m data_dog.mujoco_sim.mujoco_eval --json     # 只输出 {n,hit,accuracy}
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from data_dog.agent.loop import AgentLoop
from data_dog.mujoco_sim.to_sample import make_mujoco_batch
from data_dog.providers.client import ChatProvider
from data_dog.synth.evidence_source import SyntheticEvidenceSource
from data_dog.synth.synth_eval import (
    DeterministicSynthProvider,
    _make_candidate,
    expected_vs_actual,
    summarize_synth,
)
from data_dog.tools.investigation import InvestigationTools

# ground truth 期望（与 synth_eval 一致）：正常→rejected，各故障→需复核/确认
_FAULT_EXPECTED = {
    "normal": "rejected",
    "F1_stall": "needs_human_review",
    "F2_temp_rise": "needs_human_review",
    "F3_packet_loss": "needs_human_review",
    "F4_current_bias": "needs_human_review",
    "F5_load_spike": "needs_human_review",
}


def build_mujoco_samples(duration_s: float = 2.0, dt: float = 0.01, seed: int = 0):
    """用 MuJoCo 仿真产出一串样本：1 正常 + 5 类故障各一条。

    故障注入到"前左膝关节"（joint_motor.front_left_knee）——在本次正弦摆动
    控制下，knee/calf 是真正承受负载、升温、电流大的关节；hip 偏摆几乎不发热，
    故障若落在 hip 上，确定性 Provider 按温度/电流阈值判不出异常。
    其余部件保持正常，便于 Agent 从温差/电流差看出异常。
    """
    fault_joint = "joint_motor.front_left_knee"
    faults = ["normal"] + [f for f in _FAULT_EXPECTED if f != "normal"]
    samples = make_mujoco_batch(
        faults=faults,
        fault_component=fault_joint,
        duration_s=duration_s, dt=dt, seed=seed,
    )
    labels = []
    for i, f in enumerate(faults):
        labels.append({
            "id": f"mj{i}",
            "fault": f,
            "component": fault_joint if f != "normal" else "",
            "expected": _FAULT_EXPECTED[f],
        })
    return samples, labels


class MujocoDeterministicProvider(DeterministicSynthProvider):
    """与 synth 的 DeterministicProvider 相同，但第一步查询我们的故障关节(knee)。"""

    def __init__(self):
        super().__init__()
        self._component = "joint_motor.front_left_knee"
        self.n = 0
        self.telemetry = None

    def chat(self, messages, tools=None, model=None, temperature=0.2, max_tokens=2048):
        self.n += 1
        if self.n == 1:
            return {
                "kind": "tool_calls",
                "tool_calls": [{
                    "id": "s1", "name": "query_candidate_evidence",
                    "arguments": json.dumps({
                        "device_sn": "device-synth",
                        "component": self._component,
                        "window_start": "09:55", "window_end": "10:00",
                    }),
                    "content": "",
                }],
                "__raw_tool_calls": [],
            }
        tel = self._find_telemetry(messages)
        return {
            "kind": "text",
            "content": json.dumps(self._decide(tel), ensure_ascii=False),
        }


def run_mujoco_eval(provider: ChatProvider | Callable[[], ChatProvider] | None = None,
                    model=None,
                    duration_s: float = 2.0, dt: float = 0.01,
                    seed: int = 0) -> list[dict]:
    samples, labels = build_mujoco_samples(duration_s, dt, seed)

    results = []
    for sample, label in zip(samples, labels, strict=False):
        cand = _make_candidate(sample, label)
        source = SyntheticEvidenceSource([sample])
        tools = InvestigationTools(source)
        prov = (
            MujocoDeterministicProvider()
            if provider is None
            else (provider() if callable(provider) else provider)
        )
        agent = AgentLoop(prov, tools, model=model)
        report = agent.investigate(cand)
        d = report.disposition
        hit = expected_vs_actual(d, label)
        results.append({
            "sample": label["id"],
            "fault": label["fault"],
            "component": cand.component_ref,
            "ground_truth": label["expected"],
            "agent_disposition": d,
            "confidence": round(float(report.confidence), 2),
            "summary": report.summary[:90],
            "hit": hit,
        })
    return results


def main():
    import argparse
    p = argparse.ArgumentParser(description="MuJoCo 数据源下 Agent 调查准确率评估")
    p.add_argument("--dur", type=float, default=30.0, help="每段仿真时长（秒）")
    p.add_argument("--json", action="store_true", help="只输出 {n,hit,accuracy} JSON")
    args = p.parse_args()

    results = run_mujoco_eval(duration_s=args.dur)
    summary = summarize_synth(results)

    if args.json:
        print(json.dumps({"summary": summary,
                          "details": [{"sample": r["sample"], "fault": r["fault"],
                                       "component": r["component"],
                                       "ground_truth": r["ground_truth"],
                                       "agent_disposition": r["agent_disposition"],
                                       "confidence": r["confidence"],
                                       "hit": r["hit"]} for r in results]},
                         ensure_ascii=False))
        return
    print(f"MuJoCo 数据源评估 —— n={summary['n']} hit={summary['hit']} "
          f"accuracy={summary['accuracy']}")
    for r in results:
        print(f"  {r['sample']:<4} fault={r['fault']:<12} "
              f"gt={r['ground_truth']:<16} agent={r['agent_disposition']:<18} "
              f"hit={r['hit']}")


if __name__ == "__main__":
    main()
