"""E2E 端到端演示：厂商文档 → 算子 → 候选 → Agent 调查 → 上报 SaaS（一条命令）。

跑法：
    python -m data_dog.e2e.replay_demo          # 人读输出
    python -m data_dog.e2e.replay_demo --json   # 机器可读

全程离线、确定性（Agent 用确定性 Provider，不调 LLM），可复现。
对齐架构 §19.3「真实回放验收路径」。
"""
from __future__ import annotations

import argparse
import json
import sys

from .replay import run_e2e


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    r = run_e2e()

    if args.json:
        print(json.dumps(r.summary(), ensure_ascii=False, indent=2, default=str))
        return 0

    print("=" * 72)
    print("E2E 整链回放：厂商文档 → 算子 → 候选 → Agent 调查 → 策略门 → SaaS")
    print("=" * 72)
    print("\n[P1] 离线算子工厂")
    print(f"     算子 {r.operator_id}（{r.operator_fields} 字段 / {r.operator_rules} 规则）")
    print("\n[P2] 在线数据面")
    print(f"     收到 {r.ingested} 条 → 绑定 {r.resolved} → 失败关闭 {r.rejected} "
          f"→ 候选 {len(r.candidates)}")
    for c in r.candidates:
        print(f"       ▸ {c.candidate_id} [{c.severity_hint}] {c.component_ref} "
              f"规则={c.rule_matches}")
    print("\n[Agent] 受控调查（确定性）")
    for rep in r.reports:
        print(f"     ▸ {rep.investigation_id}: {rep.disposition} "
              f"(conf {rep.confidence:.2f})")
        print(f"       {rep.summary[:70]}")
    print("\n[P3] 上报闭环")
    for d in r.decisions:
        verdict = "✅ 上报" if d.report else "⛔ 不上报"
        print(f"     ▸ {verdict}  severity={d.severity}"
              + (f"  命中: {'; '.join(d.reasons)}" if d.report else f"  ({d.suppressed_reason})"))
    print(f"     SaaS 已接收 {r.alerts_delivered} 条正式事件；人工反馈 {len(r.feedback)} 条")
    print("\n" + "=" * 72)
    print("整链贯通 ✅  厂商文档 → 算子 → 候选 → 调查 → 策略门 → SaaS → 反馈")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
