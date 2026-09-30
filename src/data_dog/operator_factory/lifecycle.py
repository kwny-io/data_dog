"""P1：算子发布状态机（对齐架构 §8.5）。

覆盖 `Draft → Generated → Building → Validating → AwaitingApproval → Signed →
Registered → Shadow → Canary → Active`，以及分支：
  Rejected（编译/扫描/回放失败）、Quarantined（崩溃或指标越界）、RolledBack。

设计原则（对齐 §8.5 / §4.7 / §8.4）：
  - **状态转移是确定性的**：给定当前状态 + 事件，下一状态唯一可判定；
  - **门控（gate）失败拒绝前进**：如 Validating 阶段回放不通过 → 拒绝进入 AwaitingApproval；
  - **有状态算子升级必须声明状态策略**（reset / migrate / dual_run）；
  - **回滚是回到上一签名版本**，不是"删除"。

本模块是**本地可控实现**：不接真实 Registry/签名服务（那属 P4 控制面），
但把"状态合法性与门控"做成可跑、可测的确定性逻辑——对应 18 号文档 P1 第 5 项。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class TransitionError(RuntimeError):
    """非法的状态转移（确定性拒绝，对应统一失败语义 version_conflict/invalid_input）。"""


# 状态集合（§8.5 状态图 + §4.7 的隔离态）
STATES = [
    "Draft", "Generated", "Building", "Validating", "AwaitingApproval", "Signed",
    "Registered", "Shadow", "Canary", "Active",
    "Rejected", "Quarantined", "RolledBack", "Deprecated", "Retired",
]

TERMINAL = {"Retired"}

# 允许的转移（确定性台账）
_ALLOWED: dict[str, list[str]] = {
    "Draft": ["Generated"],
    "Generated": ["Building"],
    "Building": ["Validating", "Rejected"],            # 编译/扫描失败
    "Validating": ["AwaitingApproval", "Rejected"],    # 契约/回放失败
    "AwaitingApproval": ["Signed", "Rejected"],        # 人工批准 / 驳回
    "Signed": ["Registered"],
    "Registered": ["Shadow"],
    "Shadow": ["Canary", "Quarantined", "RolledBack"],  # 崩溃/偏差超限
    "Canary": ["Active", "RolledBack"],
    "Active": ["Quarantined", "Deprecated"],
    "Quarantined": ["RolledBack"],                     # 由稳定版接管
    "Deprecated": ["Retired"],
    "RolledBack": ["Registered"],                      # 可重新走注册（新 revision）
    "Rejected": ["Draft"],                             # 修正后可重来
    "Retired": [],
}

# 回滚目标阶段（§8.5：恢复前一签名版本及兼容状态）
STATUS_POLICIES = {"reset", "migrate", "dual_run"}


@dataclass
class GateResult:
    ok: bool
    reason: str = ""


@dataclass
class OperatorLifecycle:
    """一个算子版本的发布生命周期（确定性状态机）。"""
    operator_id: str
    version: str
    state: str = "Draft"
    has_state: bool = False            # 是否为有状态算子（Window/Detector/Correlator）
    state_policy: str | None = None  # reset | migrate | dual_run（有状态必填）
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise TransitionError(f"未知状态: {self.state}")
        self.history.append({"from": None, "to": self.state, "event": "init", "ok": True})

    # -- 门控：进入某状态前的前置检查 --
    def _gate(self, target: str, evidence: dict[str, Any] | None) -> GateResult:
        ev = evidence or {}
        if target == "Validating":
            # 编译/扫描必须通过。G8：`scan_findings` 可由 security_scan 产出
            # （含 severity）——任一 high/critical → 拒绝；无 findings 时回退看 scan_ok。
            findings = ev.get("scan_findings")
            if findings:
                from .security_scan import BLOCKING
                blocking = [f for f in findings
                            if getattr(f, "severity", None) in BLOCKING]
                if blocking:
                    sev = blocking[0].severity
                    return GateResult(False, f"安全扫描发现 {sev} 级问题（{len(blocking)} 项）")
            elif ev.get("scan_ok") is False:
                return GateResult(False, "安全扫描未通过")
        if target == "AwaitingApproval":
            # 验证门：Fixture 回放必须全过
            replay = ev.get("replay")
            if not replay:
                return GateResult(False, "缺少 Fixture 回放结果")
            if replay.get("passed") != replay.get("n"):
                return GateResult(False, f"回放未全过 {replay.get('passed')}/{replay.get('n')}")
            if not ev.get("why_not_lower_layer"):
                return GateResult(False, "缺少 why_not_lower_layer（§8.3 升级判据）")
        if target == "Signed":
            if ev.get("approved") is not True:
                return GateResult(False, "未获人工批准（首期人工审批，§4.10）")
        if target == "Registered":
            if not ev.get("signature"):
                return GateResult(False, "缺少制品签名（§11.3）")
            if self.has_state and self.state_policy not in STATUS_POLICIES:
                return GateResult(False, "有状态算子必须声明 state_policy（reset/migrate/dual_run）")
        if target in ("Canary", "Active"):
            # 指标门：以 evidence 表达（crash_rate / 解析成功率下降等）
            if ev.get("metrics_ok") is False:
                return GateResult(False, "灰度/指标未达标")
        return GateResult(True)

    def can_go(self, target: str) -> bool:
        return target in _ALLOWED.get(self.state, [])

    def transition(self, target: str,
                   evidence: dict[str, Any] | None = None) -> str:
        """执行一次状态转移；非法转移或门控失败 → 抛 TransitionError（确定性拒绝）。"""
        if target not in STATES:
            raise TransitionError(f"未知目标状态: {target}")
        if not self.can_go(target):
            self.history.append({"from": self.state, "to": target,
                                 "event": "transition", "ok": False,
                                 "reason": "非法转移"})
            raise TransitionError(f"{self.state} 不能直接到 {target}（§8.5）")
        gate = self._gate(target, evidence)
        if not gate.ok:
            self.history.append({"from": self.state, "to": target,
                                 "event": "transition", "ok": False,
                                 "reason": gate.reason})
            raise TransitionError(f"门控拒绝 → {target}: {gate.reason}")
        prev, self.state = self.state, target
        self.history.append({"from": prev, "to": target, "event": "transition", "ok": True})
        return self.state

    def quarantine(self, reason: str) -> str:
        return self.transition("Quarantined", {"reason": reason})

    def rollback(self, reason: str) -> str:
        return self.transition("RolledBack", {"reason": reason})


def run_happy_path(operator_id: str, version: str, *, replay: dict[str, Any],
                   signature: str, has_state: bool = False,
                   state_policy: str | None = None) -> OperatorLifecycle:
    """走一遍完整发布：Draft→…→Active（每步用合法证据）。"""
    lc = OperatorLifecycle(operator_id, version, has_state=has_state,
                           state_policy=state_policy)
    lc.transition("Generated")
    lc.transition("Building", {"scan_ok": True})
    lc.transition("Validating", {"scan_ok": True})
    lc.transition("AwaitingApproval",
                  {"replay": replay, "why_not_lower_layer": "多字段窗口，低层表达不了"})
    lc.transition("Signed", {"approved": True})
    lc.transition("Registered", {"signature": signature})
    lc.transition("Shadow")
    lc.transition("Canary", {"metrics_ok": True})
    lc.transition("Active", {"metrics_ok": True})
    return lc
