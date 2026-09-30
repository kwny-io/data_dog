"""P4-3 升级引擎：告警无人 ack → 按 SLA 逐级升级，到顶封顶（不无限升）。

闭环
----
    alert 产生
      → 查 oncall_rota(domain, now) 取当前值班人 → 投递 → 落 alert_delivery
        (status=pending, ack_deadline = now + SLA(level))
      → 后台 sweeper 周期扫：pending 且 now > ack_deadline
           → 升到 level+1（写**新** alert_delivery 行，幂等键含 level）
           → 旧行置 status=escalated
      → 有人 ack（POST /v1/alerts/{id}/ack）→ 该告警所有 pending 行置 acked，停止升级
      → 到最高 level 仍无 ack → 该级置 failed（升级链尽头，不再升）

幂等（关键）
------------
`delivery_id = hash(event_id, level)`：同一告警同一级**只投一次**。sweeper 每轮对
同一超时行重复处理，只会 UPSERT 同一 (event_id, level) 行，不产生重复投递。

为什么用"后台线程 + DB 时间戳"而非内存定时器
--------------------------------------------
ack_deadline 持久在库里：进程重启后 sweeper 接着按库里时间戳扫，**不丢计时**。
内存定时器重启即清零，会漏升。

诚实边界
--------
- **单副本正确，多副本需租约**：sweeper 是进程内线程；多副本会各自扫、并发升级
  （虽因幂等键收敛到同一行，但会重复投递）。生产多副本应加租约（与「缺口-5 多副本
  协调」同源）。
- **不做 7×24 运营**：本模块提供机制，**排班是人**的事（谁值哪班是运营决定）。
- **通知可靠性 = 适配器可靠性**（见 notifiers.py）：投递失败按 outbox 语义重试，
  **不保证必达**。
- **投递与落台账的原子性**：先落 pending 台账再投，投递结果回写该行；投递与落库
  之间崩溃 → 该行停在 pending 且 attempts=0，sweeper 到 deadline 会再投（可接受：
  宁可重复，不可漏发）。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from .. import settings
from ..storage.db import session_scope
from .notifiers import NotifierRegistry
from .oncall import RotaEntry, current_oncall, rows_to_entries

_log = logging.getLogger(__name__)

PENDING = "pending"
ACKED = "acked"
ESCALATED = "escalated"
FAILED = "failed"


def make_delivery_id(event_id: str, level: int) -> str:
    """确定性投递 id：同一 (告警, 级别) → 同一行（幂等的落点）。"""
    raw = f"{event_id}|{int(level)}".encode()
    return "dlv_" + hashlib.sha256(raw).hexdigest()[:16]


@dataclass
class DispatchResult:
    ok: bool
    delivery_id: str = ""
    level: int = 0
    channel: str = ""
    assignee: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "delivery_id": self.delivery_id,
                "level": self.level, "channel": self.channel,
                "assignee": self.assignee, "reason": self.reason}


class EscalationEngine:
    """派发 + 升级。线程安全（sweeper 与 HTTP ack 可并发）。

    `registry`（通道注册表）与 `clock`（取"现在"）可注入，便于测试与确定性。
    """

    def __init__(self, *, registry: NotifierRegistry | None = None,
                 clock=None) -> None:
        self._registry = registry or NotifierRegistry.default()
        self._clock = clock or time.time

    # ---- 元数据（从落库告警行取领域/组件）--------------------------------

    @staticmethod
    def _domain_of(alert: dict[str, Any]) -> str:
        """告警的"领域"：用 `component_ref` 的首段（如 joint_motor.front_hip → joint_motor）。

        查不到则 `"*"`（通配，全局值班兜底）。这是**确定性**的映射，不猜。
        """
        comp = str(alert.get("component_ref", "") or "")
        if not comp:
            return "*"
        return comp.split(".", 1)[0]

    # ---- 首次派发 --------------------------------------------------------

    def dispatch(self, *, tenant_id: str, alert: dict[str, Any]) -> DispatchResult:
        """为一条告警按值班表派发**首次**通知（level=1，表里最低有效级）。

        无值班人 → 失败关闭（不投递，`reason=no_oncall`）；调用方据此保留告警但不
        假装已通知。
        """
        from ..storage import repository as repo

        domain = self._domain_of(alert)
        now = float(self._clock())

        with session_scope(tenant_id=tenant_id) as s:
            entries = rows_to_entries(repo.list_rota(s, tenant_id=tenant_id))
        rota = current_oncall(entries, domain=domain, at=now, level=1)
        if rota is None:
            return DispatchResult(False, reason="no_oncall")

        return self._deliver(tenant_id=tenant_id, alert=alert, rota=rota, now=now)

    # ---- 单次投递（落台账 + 实发 + 回写）--------------------------------

    def _deliver(self, *, tenant_id: str, alert: dict[str, Any],
                 rota: RotaEntry, now: float) -> DispatchResult:
        from ..storage import repository as repo
        from .notifiers import build_notification

        event_id = str(alert.get("event_id", ""))
        level = int(rota.level)
        delivery_id = make_delivery_id(event_id, level)
        sla = settings.notify_sla_for_level(level)
        deadline = now + sla
        route = rota.to_route()
        note = build_notification(alert)

        # 先落 pending 台账（不 bump attempts——attempts 记"实际发送尝试"）
        with session_scope(tenant_id=tenant_id) as s:
            repo.upsert_delivery(
                s, tenant_id=tenant_id, delivery_id=delivery_id,
                event_id=event_id, level=level, channel=route.channel,
                target=route.target, assignee=route.assignee,
                status=PENDING, sent_at=now, ack_deadline=deadline,
                domain=str(rota.domain), bump_attempts=False)

        result = self._registry.send(note, route=route)

        with session_scope(tenant_id=tenant_id) as s:
            repo.upsert_delivery(
                s, tenant_id=tenant_id, delivery_id=delivery_id,
                event_id=event_id, level=level, channel=route.channel,
                target=route.target, assignee=route.assignee,
                status=PENDING, sent_at=now, ack_deadline=deadline,
                domain=str(rota.domain), bump_attempts=True,
                last_error=(result.detail if not result.ok else ""))

        return DispatchResult(result.ok, delivery_id=delivery_id, level=level,
                              channel=route.channel, assignee=route.assignee,
                              reason=(result.detail if not result.ok else ""))

    # ---- 升级 ------------------------------------------------------------

    def sweep_once(self, *, limit: int | None = None) -> dict[str, Any]:
        """扫一轮：把「pending 且已过 ack_deadline」的投递逐条升级。返回统计。

        **跨租户**扫描（系统级后台进程）——每条命中的行按其自带 tenant_id 单独处理。
        """
        from ..storage import repository as repo

        now = float(self._clock())
        max_level = settings.notify_max_level()

        with session_scope() as s:
            due = repo.list_due_deliveries(s, now=now, limit=limit)

        stats = {"scanned": len(due), "escalated": 0, "failed": 0,
                 "no_next_level": 0}
        for row in due:
            tenant_id = str(row.get("tenant_id", ""))
            event_id = str(row.get("event_id", ""))
            level = int(row.get("level", 1))
            delivery_id = str(row.get("delivery_id", ""))
            if not tenant_id or not event_id:
                continue

            outcome = self._escalate_row(
                tenant_id=tenant_id, event_id=event_id, level=level,
                delivery_id=delivery_id, max_level=max_level, now=now)
            stats[outcome] += 1
        return stats

    def _escalate_row(self, *, tenant_id: str, event_id: str, level: int,
                      delivery_id: str, max_level: int, now: float) -> str:
        """把一条到期投递升到 level+1；到顶则置 failed。返回统计键。"""
        from ..storage import repository as repo

        # 到顶：升级链尽头 → 该行 failed（不再升，避免无限升级）
        if level >= max_level:
            with session_scope(tenant_id=tenant_id) as s:
                repo.set_delivery_status(
                    s, tenant_id=tenant_id, delivery_id=delivery_id,
                    status=FAILED, last_error="升级链尽头：达最高级仍无 ack")
            return "failed"

        next_level = level + 1
        # 取该告警行（供领域/内容）+ 该领域下一级值班人
        with session_scope(tenant_id=tenant_id) as s:
            alert = repo.get_alert(s, event_id, tenant_id=tenant_id)
            entries = rows_to_entries(repo.list_rota(s, tenant_id=tenant_id))
        if alert is None:
            return "no_next_level"
        domain = self._domain_of(alert)
        rota = current_oncall(entries, domain=domain, at=now, level=next_level)
        if rota is None:
            # 下一级无人值班：无法升级。如实标（不静默），该行留待人工/运维处理。
            with session_scope(tenant_id=tenant_id) as s:
                repo.set_delivery_status(
                    s, tenant_id=tenant_id, delivery_id=delivery_id,
                    status=ESCALATED, last_error="下一级无值班人")
            return "no_next_level"

        # 先标旧行为 escalated，再投下一级（新行，level+1）
        with session_scope(tenant_id=tenant_id) as s:
            repo.set_delivery_status(s, tenant_id=tenant_id,
                                     delivery_id=delivery_id, status=ESCALATED)
        self._deliver(tenant_id=tenant_id, alert=alert, rota=rota, now=now)
        return "escalated"

    # ---- ack（停止升级）--------------------------------------------------

    def ack(self, *, tenant_id: str, event_id: str) -> int:
        """确认某告警：所有 pending 投递置 acked（停止升级）。返回改动条数。

        同时把告警生命周期推进到 `escalated→recovering`（若当前可一步转）——被确认
        意味着有人接手，进入恢复观察。转移不合法则只 ack、不改状态（不硬造）。
        """
        from ..alerting.lifecycle import TRANSITIONS
        from ..storage import repository as repo

        now = float(self._clock())
        with session_scope(tenant_id=tenant_id) as s:
            n = repo.ack_deliveries(s, tenant_id=tenant_id,
                                    event_id=event_id, at=now)
            alert = repo.get_alert(s, event_id, tenant_id=tenant_id)
            if alert is not None and "recovering" in TRANSITIONS.get(
                    alert["state"], set()):
                repo.set_alert_state(s, tenant_id=tenant_id, event_id=event_id,
                                     to_state="recovering", reason="值班人已 ack")
        return n


# ------------------------------------------------------------------ sweeper

class EscalationSweeper:
    """周期跑 `EscalationEngine.sweep_once` 的后台线程。

    与 `CheckpointLoop` 同样用 `Event.wait(timeout)`——停止信号能立刻唤醒，不拖满
    整个间隔才退出。
    """

    def __init__(self, engine: EscalationEngine, *,
                 interval_s: float | None = None) -> None:
        self._engine = engine
        self._interval = interval_s if interval_s is not None \
            else settings.notify_sweep_interval_s()
        self._interval = max(1.0, float(self._interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                stats = self._engine.sweep_once()
                if stats["escalated"] or stats["failed"] or stats["no_next_level"]:
                    _log.info("告警升级扫描", extra=stats)
            except Exception as e:                       # noqa: BLE001
                # 扫描失败不致命（下轮再试），但必须记 error，别静默
                _log.error("告警升级扫描失败", extra={
                    "error": f"{type(e).__name__}: {e}"})

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="dd-escalation",
                                        daemon=True)
        self._thread.start()
        _log.info("告警升级 sweeper 已启动", extra={"interval_s": self._interval})

    def stop(self) -> None:
        self._stop.set()
