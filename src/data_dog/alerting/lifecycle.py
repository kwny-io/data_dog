"""P3 Alert 生命周期状态机（对齐架构 §14.3）。

    Detected → Confirmed / Suppressed / Rejected
    Confirmed → Escalated / Recovering
    Escalated → Recovering
    Recovering → Resolved
    Resolved → Closed
    Suppressed → Detected（条件恶化）
    Resolved → Detected（同 Episode 复发）

要点：
  - 状态转移**确定性**：非法转移抛错（不静默）。
  - 同一 Episode 更新沿用**同一 event_id**，只递增 `event_revision`（§14.3 末）。
  - 每次转移留审计历史（谁/何时/为什么）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# 允许的状态转移表（from → 可去的 to 集合）
TRANSITIONS: dict[str, set] = {
    "detected":   {"confirmed", "suppressed", "rejected"},
    "confirmed":  {"escalated", "recovering", "closed"},
    "escalated":  {"recovering", "closed"},
    "recovering": {"resolved", "escalated"},
    "resolved":   {"closed", "detected"},     # 同 Episode 复发 → detected
    "suppressed": {"detected"},               # 条件恶化 → 重新检测
    "rejected":   {"detected"},               # 误报后同 Episode 再出现
    "closed":     set(),                      # 终态
}

STATES = set(TRANSITIONS)

# 哪些状态算"活动事件"（用于策略门判断是否新建 vs 更新）
ACTIVE_STATES = {"detected", "confirmed", "escalated", "recovering"}


class AlertTransitionError(ValueError):
    """非法的生命周期转移。"""


@dataclass
class AlertEvent:
    """一份正式事件（对齐 §12.5 SaaS AlertEvent 的本地模型）。"""
    event_id: str                         # 同一 Episode 复用
    idempotency_key: str
    device_ref: str
    component_ref: str
    event_type: str
    state: str = "detected"
    severity: str = "warning"
    confidence: float = 0.0
    detected_at: str = ""
    event_revision: int = 1
    summary: str = ""
    recommended_actions: list[str] = field(default_factory=list)
    evidence_receipt: str = ""
    investigation_id: str = ""
    policy_version: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        return d


class AlertLifecycle:
    """管理单个 AlertEvent 的状态转移（确定性 + 审计历史）。"""

    def __init__(self, event: AlertEvent):
        self.event = event
        self.event.history.append({
            "from": None, "to": event.state,
            "reason": "created", "at": time.time(),
            "revision": event.event_revision,
        })

    def can(self, to_state: str) -> bool:
        return to_state in TRANSITIONS.get(self.event.state, set())

    def transition(self, to_state: str, *, reason: str = "") -> AlertEvent:
        if to_state not in STATES:
            raise AlertTransitionError(f"未知状态: {to_state!r}")
        if not self.can(to_state):
            raise AlertTransitionError(
                f"{self.event.state} 不能直接到 {to_state}（§14.3）")
        frm = self.event.state
        self.event.state = to_state
        self.event.event_revision += 1        # 同一 event_id，递增 revision
        self.event.history.append({
            "from": frm, "to": to_state, "reason": reason,
            "at": time.time(), "revision": self.event.event_revision,
        })
        return self.event

    def is_active(self) -> bool:
        return self.event.state in ACTIVE_STATES
