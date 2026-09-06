# -*- coding: utf-8 -*-
"""data_dog Agent 评估命令行入口。

用法：
  python -m evals.run_evals --mode mock      # 离线确定性评估（默认，不联网）
  python -m evals.run_evals --mode real      # 用已配置的真实 LLM 评估（花 token）
  python -m evals.run_evals --only case_01   # 只跑某个 case
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class MockDecisionProvider:
    """一个"理想行为"的脚本 provider：体现我们希望 Agent 具备的推理。

    这是 eval 的确定性基线：用它时，框架能验证"评分逻辑 + 用例定义"是否正确
    （即：用例期望的行为，能被正确评估为通过）。真实模式再用真 LLM 观察实际差距。
    """

    def __init__(self, case: str):
        self.case = case
        self.n = 0

    def chat(self, messages, tools=None, model=None, temperature=0.2, max_tokens=2048):
        self.n += 1
        # 模型输出非法 JSON：验证安全降级（不崩溃、不装确认）
        if self.case == "case_05_invalid_json":
            return {
                "kind": "text",
                "content": '{"disposition": "confirmed", "confidence": 0.9, "su',  # 截断的 JSON
            }
        # 证据不足 / 越权部件：无法查证，直接 needs_human_review，绝不 confirmed
        if self.case in ("case_02_no_evidence", "case_03_denied_component"):
            label = "无可用遥测证据" if self.case == "case_02_no_evidence" else "该部件越权不可访问"
            return {
                "kind": "text",
                "content": json.dumps({
                    "disposition": "needs_human_review",
                    "confidence": 0.2,
                    "summary": f"{label}，无法确认异常，需人工复核。",
                    "hypotheses": [],
                    "missing_evidence": [label, "原始日志"],
                }, ensure_ascii=False),
            }
        # 正常数据（benign）：查一次证据后判 rejected（排除），而不是无端确认
        if self.case == "case_04_benign":
            if self.n == 1:
                return {"kind": "tool_calls", "tool_calls": [{
                    "id": "m4", "name": "query_candidate_evidence",
                    "arguments": json.dumps({"device_sn": "device-eval",
                                             "component": "joint_motor.front_right_hip",
                                             "window_start": "09:55", "window_end": "10:00"}),
                    "content": ""}], "__raw_tool_calls": []}
            return {"kind": "text", "content": json.dumps({
                "disposition": "rejected",
                "confidence": 0.7,
                "summary": "该部件遥测正常（温度61℃/电流6.2A，均在基线内），排除异常。",
                "hypotheses": [],
            }, ensure_ascii=False)}
        # case_01：先查证据 → 再查厂商知识 → 最后 confirmed
        if self.n == 1:
            return {"kind": "tool_calls", "tool_calls": [{
                "id": "m1", "name": "query_candidate_evidence",
                "arguments": json.dumps({"device_sn": "device-eval",
                                         "component": "joint_motor.front_left_hip",
                                         "window_start": "09:55", "window_end": "10:00"}),
                "content": ""}], "__raw_tool_calls": []}
        if self.n == 2:
            return {"kind": "tool_calls", "tool_calls": [{
                "id": "m2", "name": "query_vendor_knowledge",
                "arguments": json.dumps({"component_type": "joint_motor"}),
                "content": ""}], "__raw_tool_calls": []}
        return {"kind": "text", "content": json.dumps({
            "disposition": "confirmed",
            "event_type": "joint_motor_thermal_degradation",
            "severity": "warning",
            "confidence": 0.85,
            "summary": "温度78.2℃超75℃阈值且电流超基线62%，符合热退化。",
            "hypotheses": [{"cause": "bearing_friction_or_cooling_degradation", "status": "supported"}],
            "missing_evidence": ["ambient_temperature"],
        }, ensure_ascii=False)}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="data_dog Agent 评估")
    parser.add_argument("--mode", choices=["mock", "real"], default="mock")
    parser.add_argument("--only", default=None, help="只跑某个 case_id")
    parser.add_argument("--model", default=None, help="real 模式下指定模型（覆盖配置）")
    args = parser.parse_args()

    from evals.cases import CASES, find_case
    from evals.runner import run_all, run_case, summarize

    only = [args.only] if args.only else None

    if args.mode == "mock":
        providers = {c.case_id: MockDecisionProvider(c.case_id) for c in CASES}
        if only:
            results = [run_case(find_case(only[0]), providers[only[0]])]
        else:
            results = []
            for c in CASES:
                results.append(run_case(c, providers[c.case_id]))
    else:
        from data_dog.providers.config import load_config
        from data_dog.providers.client import Provider
        cfg = load_config()
        key = cfg["api_key"]
        if not key:
            print("real 模式需要先配置 API key：data-dog init")
            sys.exit(1)
        provider = Provider.from_config(cfg["vendor_id"], key)
        results = run_all(provider, model=args.model or cfg.get("model") or None, only=only)

    print("\n===== Agent 评估结果 =====")
    for r in results:
        print(f"\n[{r['case_id']}] {r['name']}")
        print(f"  裁决={r['disposition']} | 置信度={r['confidence']} | 评分={r['score']}")
        print(f"  checks: {json.dumps(r['checks'], ensure_ascii=False)}")
        print(f"  摘要: {r['summary']}")

    s = summarize(results)
    print(f"\n===== 汇总 ===== {s['passed']}/{s['n']} 通过，通过率 {s['pass_rate']}")
    return 0 if s["n"] > 0 and s["passed"] == s["n"] else 1


if __name__ == "__main__":
    sys.exit(main())
