"""反馈→评估回灌：把人工反馈当作**评估标签**，校准对 Agent 的度量。

为什么需要它（缺口：反馈未回灌评估）
------------------------------------
此前人工反馈（误报/已修）只做两件事：落库、改告警状态。但架构 §6.5 的"反馈闭环"
真义是——**人工判定要能反过来衡量 Agent 判得准不准**。否则"反馈"只是存档，Agent
永远不会因人工纠正而被度量、被改进。

本模块把三类证据接起来（都是**已落库的真实数据**，非臆造）：
    alert_feedback.kind   ← 人工判定（误报 / 已修 / 无需处理）
    reports.disposition   ← Agent 当初的裁决（confirmed / rejected / needs_human_review…）
    alerts.investigation_id ← 二者经它关联（同一 Episode）

人工判定 → 评估口径（ground truth）
-----------------------------------
- `false_positive`（误报）→ 该告警**本不该报** → 真值 = rejected
- `repaired`（已修）      → 该告警**确有其事** → 真值 = confirmed
- `no_action`（无需处理） → **不作标签**（语义模糊：既可能是"小问题不改"也可能是"误报"），
  计入 unlabeled，**不混进准确率分子分母**——宁可少算，不硬凑数字。

Agent 裁决 vs 真值的判定（确定性）
----------------------------------
- agree           ：裁决 ∈ 真值集合（如裁决 rejected、真值 rejected）
- false_positive  ：真值 rejected 但 Agent 判 confirmed（**误报**：报了不该报的）
- false_negative  ：真值 confirmed 但 Agent 判 rejected（**漏报**：该报没报）
- escalated       ：裁决 needs_human_review/inconclusive → 既非对也非错，交由人工，
                    单列（不计入 agree，也不计入误报/漏报）
"""
from __future__ import annotations

from typing import Any

# 人工判定 → 评估真值。no_action 刻意不在表内（不作标签）。
_HUMAN_TO_TRUTH: dict[str, str] = {
    "false_positive": "rejected",
    "repaired": "confirmed",
}

# 视为"上报了故障"的裁决（数字真值 confirmed 时，这些算命中；数字真值 rejected 时算误报）
_CLAIMS_FAULT = {"confirmed"}
# 视为"排除了故障"的裁决
_CLAIMS_BENIGN = {"rejected"}
# 交由人工的裁决（既非对也非错）
_ESCALATED = {"needs_human_review", "inconclusive"}


def classify(agent_disposition: str, kind: str) -> str:
    """把一条「人工反馈 + Agent 当初裁决」归到一个确定类别。

    Returns: `agree` | `false_positive` | `false_negative` | `escalated` | `unlabeled`
    """
    truth = _HUMAN_TO_TRUTH.get(kind)
    if truth is None:
        return "unlabeled"           # no_action / 未知 kind → 不作标签
    d = (agent_disposition or "").strip()
    if d in _ESCALATED:
        return "escalated"
    if truth == "rejected":
        if d in _CLAIMS_BENIGN:
            return "agree"
        if d in _CLAIMS_FAULT:
            return "false_positive"   # 人工说误报，Agent 却确认了 → 误报
        return "unlabeled"
    # truth == confirmed：人工说确有其事
    if d in _CLAIMS_FAULT:
        return "agree"
    if d in _CLAIMS_BENIGN:
        return "false_negative"       # 人工说真故障，Agent 却排除了 → 漏报
    return "unlabeled"


def summarize_feedback(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把「反馈×Agent裁决」的明细聚合成可展示的度量。

    入参 rows：每行含 `kind`（人工判定）、`agent_disposition`（Agent 裁决），
    可选 `event_id` / `investigation_id` 等透传字段。

    指标（分母只含**有标签**的行——即被人工明确判为误报/已修的）：
      - labeled        ：有标签的行数
      - agree          ：AI 与人工一致
      - agreement_rate ：agree / labeled
      - false_positive_rate：Agent 误报数 / labeled
      - false_negative_rate：Agent 漏报数 / labeled
      - escalated_rate ：交由人工的比例（分母用全部行，因为"是否转人工"是覆盖率问题）
    """
    counts = {"agree": 0, "false_positive": 0, "false_negative": 0,
              "escalated": 0, "unlabeled": 0}
    for r in rows:
        counts[classify(str(r.get("agent_disposition", "")), str(r.get("kind", "")))] += 1

    labeled = counts["agree"] + counts["false_positive"] + counts["false_negative"]
    total = len(rows)

    def _rate(n: int, denom: int) -> float:
        return round(n / denom, 3) if denom else 0.0

    return {
        "total": total,
        "labeled": labeled,
        "agree": counts["agree"],
        "false_positive": counts["false_positive"],
        "false_negative": counts["false_negative"],
        "escalated": counts["escalated"],
        "unlabeled": counts["unlabeled"],
        "agreement_rate": _rate(counts["agree"], labeled),
        "false_positive_rate": _rate(counts["false_positive"], labeled),
        "false_negative_rate": _rate(counts["false_negative"], labeled),
        "escalated_rate": _rate(counts["escalated"], total),
    }
