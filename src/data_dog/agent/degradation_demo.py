"""降级路径演示（对齐架构 §9.4）。

跑法：
    python -m data_dog.agent.degradation_demo          # 人读输出
    python -m data_dog.agent.degradation_demo --json   # 机器可读

演示内容：
  1. Agent 在线：普通候选 → investigate_now
  2. Agent 掉线：普通候选 → queue_for_agent（进有界队列）
  3. 硬规则候选：无论 Agent 是否在线 → direct_report（fail-safe）
  4. 队列容量上限：溢出候选退化为 direct_report（不无限堆积）
  5. Agent 恢复：drain 取回排队候选，重新走调查

数据来源：复用项目既有的候选样例 `evidence/candidates/*.json`，
不另造 mock，保证演示与真实链路同源。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..core.console import ensure_utf8_console
from ..schemas.events import CandidateEvent
from .degradation import (
    DIRECT_REPORT,
    INVESTIGATE_NOW,
    QUEUE_FOR_AGENT,
    DegradationPolicy,
)

_CAND_DIR = Path(__file__).resolve().parent.parent.parent.parent / "evidence" / "candidates"


def _load_candidates() -> list[CandidateEvent]:
    """读取项目既有候选样例；找不到则用内置样例兜底（保证 demo 永远可跑）。"""
    out: list[CandidateEvent] = []
    if _CAND_DIR.is_dir():
        for p in sorted(_CAND_DIR.glob("*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            # 样例可能是单条 dict，也可能是 list
            for item in (raw if isinstance(raw, list) else [raw]):
                if isinstance(item, dict) and "candidate_id" in item:
                    try:
                        out.append(CandidateEvent(**item))
                    except TypeError:
                        continue
    if out:
        return out
    # 兜底：三个覆盖三类路径的样例
    return [
        CandidateEvent(candidate_id="normal", device_sn="device-abc123",
                       component_ref="battery.main", signal="soc_normal",
                       severity_hint="warning", window_start="09:55",
                       window_end="10:00", sample_count=100, rule_matches=[]),
        CandidateEvent(candidate_id="thermal", device_sn="device-abc123",
                       component_ref="joint_motor.front_left_hip", signal="temp_rising",
                       severity_hint="warning", window_start="09:55",
                       window_end="10:00", sample_count=100, rule_matches=[]),
        CandidateEvent(candidate_id="safety", device_sn="device-abc123",
                       component_ref="controller.main", signal="EMERGENCY_SHUTDOWN",
                       severity_hint="critical", window_start="09:58",
                       window_end="10:00", sample_count=100, rule_matches=["SAFETY"]),
    ]


def main() -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cands = _load_candidates()
    # 保证演示里一定出现一条硬规则候选（否则第 3/4 步无内容）
    if not any(c.severity_hint == "critical" or "FAULT" in c.signal.upper()
               or "EMERGENCY" in c.signal.upper() or "STALL" in c.signal.upper()
               for c in cands):
        cands.append(CandidateEvent(
            candidate_id="safety-demo", device_sn="device-abc123",
            component_ref="controller.main", signal="OVERTEMP_FAULT",
            severity_hint="critical", window_start="09:58",
            window_end="10:00", sample_count=100, rule_matches=["FAULT"]))

    steps: list[dict[str, Any]] = []

    # --- 1. Agent 在线 ---
    up = DegradationPolicy(agent_available=True)
    online = [up.decide(c) for c in cands]
    steps.append({"scenario": "Agent 在线", "decisions": [d.to_dict() for d in online]})

    # --- 2. Agent 掉线 ---
    down = DegradationPolicy(agent_available=False, queue_capacity=1)
    offline = [down.decide(c) for c in cands]
    steps.append({
        "scenario": "Agent 掉线（queue_capacity=1，观察溢出）",
        "decisions": [d.to_dict() for d in offline],
        "queued": down.queued,
    })

    # --- 3. Agent 恢复 ---
    recovered_ids = down.drain()
    down.agent_available = True
    replay = [down.decide(c) for c in cands if c.candidate_id in set(recovered_ids)]
    steps.append({
        "scenario": "Agent 恢复（drain 取回排队候选重跑）",
        "recovered": recovered_ids,
        "decisions": [d.to_dict() for d in replay],
    })

    if args.json:
        print(json.dumps({"steps": steps}, ensure_ascii=False, indent=2))
        return 0

    # ASCII 标记：Windows GBK 控制台编码不了 emoji，这里不依赖终端设置
    icon = {DIRECT_REPORT: "[!]", QUEUE_FOR_AGENT: "[~]", INVESTIGATE_NOW: "[?]"}
    print("=" * 70)
    print("Agent 降级路径演示（架构 §9.4）")
    print("=" * 70)
    for step in steps:
        print(f"\n【{step['scenario']}】")
        for d in step["decisions"]:
            mark = icon.get(d["path"], "?")
            print(f"   {mark} {d['candidate_id']:<14} → {d['path']}")
            print(f"      {d['reason']}")
        if step.get("queued"):
            print(f"   队列内容: {step['queued']}")
        if step.get("recovered") is not None:
            print(f"   取回: {step['recovered']}")
    print("\n" + "-" * 70)
    print("要点：硬规则候选始终 direct_report（不依赖 Agent）；")
    print("     普通候选在 Agent 掉线时排队、队列满则退化为直报；")
    print("     Agent 恢复后排队候选可 drain 回来继续调查。")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
