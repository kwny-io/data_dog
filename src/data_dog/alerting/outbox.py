"""P3 幂等 Outbox + 幂等键构成规则（对齐架构 §16.1 / 缺口 E-7）。

架构 §16.1：接入与内部流"至少一次投递"，外部副作用必须通过**幂等键**实现一次逻辑效果。
§12.5 的 SaaS AlertEvent 必填 `idempotency_key`，但原文只给了示例
（`device:event_type:episode-start`）——本模块把**构成规则**定死。

幂等键构成（Episode 级）：
    {device_ref}:{event_type}:{episode_key}
  - device_ref   ：受控设备引用（非明文 SN）
  - event_type   ：语义事件类型（如 joint_motor_thermal_degradation）
  - episode_key  ：同一"故障 Episode"的稳定标识（默认取 investigation_id 或 component_ref）

同一逻辑事件重复投递 → 同一个键 → outbox 去重 → 只产生一次外部效果；
但**同一 Episode 的状态更新（revision 递增）不算重复**，用 (key, revision) 区分。

投递重试预算（次数 + 墙钟双上限 + 抖动退避）
--------------------------------------------
退避与上限放在**模块内**而不是交给调用方：此前 `deliver()` 只数次数（3 次），
两次尝试的间隔完全由"调用方多久再调一次 deliver()"决定——调用方紧挨着连调就变成
毫秒级连打同一下游（把对方的瞬时故障放大成雪崩），而调用方隔很久再调又让"3 次"
跨了几个小时。这两头都不是我们想要的语义。

现在：`deliver()` 在**单次调用内**按同一个 `(幂等键, revision)` 重试，间隔是抖动的
指数退避；次数（`max_attempts`）或墙钟（`retry_deadline_s`）**先到为准**，任一耗尽
即进死信并记 `dead_reason` / `stats["deadline_exceeded"]`——失败可观测，不是静默
消失。**至少一次**语义不变：条目在成功或进死信前一直是 `pending`，且幂等键/revision
每次重试都相同（去重由 (key, revision) 保证，重复投递仍只产生一次外部效果）。
"""
from __future__ import annotations

import builtins
import hashlib
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)

# 重试预算默认值：次数 3（沿用既有默认）+ 墙钟 2s（**先到为准**）+ 抖动退避。
# 为什么墙钟只要 2s：`deliver()` 现在在调用方线程里同步跑（HTTP 上报路径），
# 2s 是"给下游一次真机会、但绝不把请求拖长"的量级；而单次投递本身已有超时
# （通知通道 5s / 本地适配器毫秒级），故 2s 足够容纳 3 次快速尝试。
_RETRY_DEADLINE_S = 2.0
_RETRY_BASE_SLEEP_S = 0.05
_RETRY_MAX_SLEEP_S = 0.5


def make_idempotency_key(device_ref: str, event_type: str, episode_key: str) -> str:
    """幂等键构成规则（§12.5 示例的规范化实现）。"""
    if not device_ref or not event_type or not episode_key:
        raise ValueError("幂等键三要素（device/event_type/episode）均不能为空")
    return f"{device_ref}:{event_type}:{episode_key}"


def make_event_id(device_ref: str, event_type: str, episode_key: str) -> str:
    """正式事件的 event_id：由**同一组三要素**确定性派生（§14.3）。

    与幂等键同源（`device_ref:event_type:episode_key`）——同一 Episode 反复调查
    得到**同一 event_id**，因此"再次上报"是**更新**（revision 递增）而非新建。
    哈希后取 16 位十六进制，避免 event_id 里出现设备/事件明细。
    """
    ikey = make_idempotency_key(device_ref, event_type, episode_key)
    return "alert_" + hashlib.sha256(ikey.encode()).hexdigest()[:16]


def episode_key_from(investigation_id: str = "", component_ref: str = "") -> str:
    """Episode 稳定标识：优先用调查 id，退化用部件名。"""
    return investigation_id or component_ref or "unknown-episode"


def make_device_ref(device_sn: str) -> str:
    """把设备明文 SN 映射为**稳定的不透明引用**（§12.5 要求非明文）。

    对外事件里不该出现设备序列号；但同一设备必须每次得到**同一**引用，否则
    幂等键/event_id 会漂移，同一 Episode 会被当成新事件。用 `device-<sha256[:16]>`：
    确定性强、不可反推。
    """
    if not device_sn:
        return "device-unknown"
    return "device-" + hashlib.sha256(device_sn.encode()).hexdigest()[:16]


@dataclass
class OutboxEntry:
    idempotency_key: str
    revision: int
    payload: dict[str, Any]
    status: str = "pending"          # pending | delivered | failed | dead
    attempts: int = 0
    receipt: str = ""
    created_at: float = field(default_factory=time.time)
    last_error: str = ""
    # 重试预算的可观测字段：首次尝试时刻（墙钟上限的起点）、最后尝试时刻、
    # 以及"为什么进死信"（max_attempts / retry_deadline）。都在 `list()` 里可见。
    first_attempt_at: float = 0.0
    last_attempt_at: float = 0.0
    dead_reason: str = ""

    @property
    def dedup_id(self) -> str:
        """去重标识：同键同 revision 才算"同一条投递"。"""
        h = hashlib.sha256(f"{self.idempotency_key}#{self.revision}".encode()).hexdigest()
        return "alertout_" + h[:12]


class AlertOutbox:
    """幂等投递箱：至少一次投递 + 按 (幂等键, revision) 去重 + 死信。

    重试在**单次 `deliver()` 内**完成，受"次数 + 墙钟"双上限约束，间隔是抖动的
    指数退避（见模块 docstring）。`sleep`/`rand` 可注入，便于测试把等待变确定。
    """

    def __init__(self, max_attempts: int = 3, *,
                 retry_deadline_s: float = _RETRY_DEADLINE_S,
                 base_sleep_s: float = _RETRY_BASE_SLEEP_S,
                 max_sleep_s: float = _RETRY_MAX_SLEEP_S,
                 sleep: Callable[[float], None] = time.sleep,
                 rand: Callable[[], float] = random.random):
        self.max_attempts = max(1, int(max_attempts))
        self.retry_deadline_s = max(0.0, float(retry_deadline_s))
        self.base_sleep_s = max(0.0, float(base_sleep_s))
        self.max_sleep_s = max(0.0, float(max_sleep_s))
        self._sleep = sleep
        self._rand = rand
        self._entries: dict[str, OutboxEntry] = {}
        self.stats = {"enqueued": 0, "duplicate": 0, "delivered": 0,
                      "failed": 0, "dead": 0,
                      # 可观测补充：总重试次数 + "因墙钟耗尽而死"的条数
                      "retries": 0, "deadline_exceeded": 0}

    # ---- 退避 ----

    def retry_backoff_s(self, attempts: int) -> float:
        """第 `attempts` 次尝试失败后的退避秒数（指数 + 单次上限 + `U(0.5,1.5)` 抖动）。

        抖动的作用与 `state/redis.py` 的冲突重试一致：多个 outbox（多副本/多租户）
        同时失败时错开重投时刻，避免恢复瞬间一齐打上下游。
        """
        n = max(1, int(attempts))
        nominal = min(self.max_sleep_s, self.base_sleep_s * (2 ** min(n - 1, 6)))
        return nominal * (0.5 + self._rand())

    def enqueue(self, idempotency_key: str, revision: int,
                payload: dict[str, Any]) -> OutboxEntry:
        e = OutboxEntry(idempotency_key=idempotency_key, revision=revision,
                        payload=payload)
        if e.dedup_id in self._entries:
            self.stats["duplicate"] += 1
            return self._entries[e.dedup_id]     # 幂等：同键同 revision → 同一条
        self._entries[e.dedup_id] = e
        self.stats["enqueued"] += 1
        return e

    def pending(self) -> builtins.list[OutboxEntry]:
        return [e for e in self._entries.values() if e.status == "pending"]

    def deliver(self, adapter) -> dict[str, int]:
        """向适配器投递所有 pending；单次调用内按"次数 + 墙钟"重试到成功或死信。

        返回本次的结果计数：`delivered` 条数、`failed` **重试次数**、`dead` 条数。
        """
        result = {"delivered": 0, "failed": 0, "dead": 0}
        for e in list(self.pending()):
            self._deliver_one(e, adapter, result)
        return result

    def _deliver_one(self, entry: OutboxEntry, adapter: Any,
                     result: dict[str, int]) -> None:
        """单条投递：失败 → 抖动退避后重试，直到成功或预算耗尽（先到为准）。"""
        if not entry.first_attempt_at:
            entry.first_attempt_at = time.time()
        while True:
            entry.attempts += 1
            entry.last_attempt_at = time.time()
            ok, resp = adapter.deliver(entry.payload,
                                       idempotency_key=entry.idempotency_key,
                                       revision=entry.revision)
            if ok:
                entry.status = "delivered"
                entry.receipt = resp.get("receipt_id", "")
                self.stats["delivered"] += 1
                result["delivered"] += 1
                return
            entry.last_error = str(resp.get("error", "unknown"))
            if entry.attempts >= self.max_attempts:
                self._mark_dead(entry, "max_attempts", result)
                return
            if time.time() - entry.first_attempt_at >= self.retry_deadline_s:
                self._mark_dead(entry, "retry_deadline", result)
                return
            self.stats["failed"] += 1
            self.stats["retries"] += 1
            result["failed"] += 1
            self._sleep(self.retry_backoff_s(entry.attempts))

    def _mark_dead(self, entry: OutboxEntry, reason: str,
                   result: dict[str, int]) -> None:
        """进死信：终态 + 原因可查（不静默丢弃，也不无限重试）。"""
        entry.status = "dead"
        entry.dead_reason = reason
        self.stats["dead"] += 1
        result["dead"] += 1
        if reason == "retry_deadline":
            self.stats["deadline_exceeded"] += 1
        _log.warning("outbox 投递进死信", extra={
            "idempotency_key": entry.idempotency_key, "revision": entry.revision,
            "attempts": entry.attempts, "reason": reason,
            "last_error": entry.last_error})

    def list(self) -> builtins.list[dict[str, Any]]:
        return [e.__dict__ for e in self._entries.values()]

    def __len__(self) -> int:
        return len(self._entries)
