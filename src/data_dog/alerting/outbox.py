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
"""
from __future__ import annotations

import builtins
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any


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

    @property
    def dedup_id(self) -> str:
        """去重标识：同键同 revision 才算"同一条投递"。"""
        h = hashlib.sha256(f"{self.idempotency_key}#{self.revision}".encode()).hexdigest()
        return "alertout_" + h[:12]


class AlertOutbox:
    """幂等投递箱：至少一次投递 + 按 (幂等键, revision) 去重 + 死信。"""

    def __init__(self, max_attempts: int = 3):
        self.max_attempts = max_attempts
        self._entries: dict[str, OutboxEntry] = {}
        self.stats = {"enqueued": 0, "duplicate": 0, "delivered": 0,
                      "failed": 0, "dead": 0}

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
        """向适配器投递所有 pending；失败重试到上限后进死信。"""
        result = {"delivered": 0, "failed": 0, "dead": 0}
        for e in list(self.pending()):
            e.attempts += 1
            ok, resp = adapter.deliver(e.payload, idempotency_key=e.idempotency_key,
                                       revision=e.revision)
            if ok:
                e.status = "delivered"
                e.receipt = resp.get("receipt_id", "")
                self.stats["delivered"] += 1
                result["delivered"] += 1
            else:
                e.last_error = str(resp.get("error", "unknown"))
                if e.attempts >= self.max_attempts:
                    e.status = "dead"
                    self.stats["dead"] += 1
                    result["dead"] += 1
                else:
                    self.stats["failed"] += 1
                    result["failed"] += 1
        return result

    def list(self) -> builtins.list[dict[str, Any]]:
        return [e.__dict__ for e in self._entries.values()]

    def __len__(self) -> int:
        return len(self._entries)
