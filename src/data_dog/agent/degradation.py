"""Agent 降级路径：Agent 不可用时候选事件如何处置（对齐架构 §9.4 + §16.4）。

为什么需要它
------------
D 现有链路默认「Agent 在线」：候选进 `AgentLoop.investigate` 出报告，
再由 `alerting.ReportPolicyGate` 决定上不上报（§14.2）。但 §9.4 要求：
**Agent 不可用时热路径不能停摆**——严重问题必须照样上报，普通候选不能丢，
也不能无限堆积。

本模块把 §9.4 做成**确定性决策表**（纯函数式、零外部依赖、可穷举可测试）：

  1. 命中严重硬规则（厂商明确错误码 / 停机条件 / 本体安全事件）
     → 绕过 Agent，`direct_report`，确定性上报（fail-safe）。
  2. Agent 不可用且未命中硬规则 → `queue_for_agent`，进有界队列等恢复；
     队列满则退化为 `direct_report`（只保聚合证据，不丢不堆）。
  3. Agent 可用 → `investigate_now`，交付正常调查。

与 §14.2 的关系：本模块决定**候选该不该进调查**（上游分流），
`alerting/policy.py` 决定**调查完了该不该上报**（下游门控）。两者互补。

与真实队列的关系：本模块只做**决策**，不做传输。队列是可注入的有界结构，
接入真实 MQ（Kafka / NATS / Redis Stream）时替换 `_queue` 即可，决策逻辑不变。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 严重硬规则信号：命中任一即视为"确定性上报"路径（fail-safe）。
# 刻意用明确的错误码/事件特征，不用过于宽泛的模式（如单字符 "E_"），
# 避免误伤正常信号把普通候选也走成直报。
HARD_RULES_HINTS = [
    "OVERTEMP", "E_STALL", "STALL", "OVERVOLT", "UNDERVOLT",
    "EMERGENCY", "FAULT", "SHUTDOWN", "TRIP", "SAFETY",
]

# 处置路径枚举（字符串常量，便于日志/序列化）
DIRECT_REPORT = "direct_report"        # 绕过 Agent 确定性上报
QUEUE_FOR_AGENT = "queue_for_agent"    # 进有界队列等 Agent 恢复
INVESTIGATE_NOW = "investigate_now"    # 正常交给 Agent 调查

PATHS = (DIRECT_REPORT, QUEUE_FOR_AGENT, INVESTIGATE_NOW)


@dataclass
class DegradationDecision:
    """对单个候选事件的处置决策。"""
    candidate_id: str
    path: str                              # 见 PATHS
    reason: str
    severity_hint: str = ""

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "path": self.path,
            "reason": self.reason,
            "severity_hint": self.severity_hint,
        }


def is_hard_rule(signal: str, rule_matches: list[str],
                 severity_hint: str = "") -> bool:
    """命中硬规则：严重度 critical，或规则名/信号含严重错误码特征。

    公开为函数（而非私有）以便 `alerting/policy.py` 与评估侧复用同一判据，
    避免两处各写一份阈值导致语义漂移。
    """
    if severity_hint == "critical":
        return True
    joined = f"{signal} {' '.join(rule_matches or [])}".upper()
    return any(h in joined for h in HARD_RULES_HINTS)


@dataclass
class DegradationPolicy:
    """给定候选与 Agent 可用性，决定处置路径。

    Args:
        agent_available: Agent（LLM 调查链路）当前是否可用。
        queue_capacity: 排队上限；超出则退化为直接上报，保证「不无限堆积」。
    """
    agent_available: bool = True
    queue_capacity: int = 100
    _queue: list[str] = field(default_factory=list, repr=False)

    @property
    def queued(self) -> list[str]:
        """当前排队中的候选 id（副本，外部不能改内部状态）。"""
        return list(self._queue)

    def decide(self, candidate) -> DegradationDecision:
        """对单个候选做出处置决策（纯函数式，除排队副作用外无隐藏状态）。

        candidate 需具备：candidate_id / signal / rule_matches / severity_hint。
        """
        cid = candidate.candidate_id

        # 1) 硬规则优先：无论 Agent 是否可用都直报（fail-safe）。
        if is_hard_rule(candidate.signal, candidate.rule_matches,
                        getattr(candidate, "severity_hint", "")):
            return DegradationDecision(
                cid, DIRECT_REPORT,
                "命中严重硬规则（严重错误码/停机条件/安全事件），确定性上报，不依赖 Agent。",
                candidate.severity_hint)

        # 2) Agent 不可用：排队；队列满则退化为直报。
        if not self.agent_available:
            if len(self._queue) >= self.queue_capacity:
                return DegradationDecision(
                    cid, DIRECT_REPORT,
                    "Agent 不可用且候选队列已满，仅保留聚合证据按严重度直接上报。",
                    candidate.severity_hint)
            self._queue.append(cid)
            return DegradationDecision(
                cid, QUEUE_FOR_AGENT,
                "Agent 不可用，中低风险候选排队等待 Agent 恢复后复核。",
                candidate.severity_hint)

        # 3) Agent 可用：正常调查。
        return DegradationDecision(
            cid, INVESTIGATE_NOW,
            "Agent 可用，交付正常调查。", candidate.severity_hint)

    def decide_batch(self, candidates) -> list[DegradationDecision]:
        """批量决策；顺序即候选顺序，便于与上游候选清单对齐。"""
        return [self.decide(c) for c in candidates]

    def drain(self) -> list[str]:
        """取出并清空队列（Agent 恢复后取回待复核候选）。"""
        out = list(self._queue)
        self._queue.clear()
        return out

    def reset(self) -> None:
        """清空队列（测试/演示用）。"""
        self._queue.clear()
