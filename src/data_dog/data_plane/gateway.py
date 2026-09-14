# -*- coding: utf-8 -*-
"""P2 接入网关（对齐架构 §9.1）。

职责：
  - 认证（桩）：从**认证上下文**注入 tenant_id/device_sn；**不信任日志正文里的身份字段**。
  - 前置检查：大小/编码/必需字段/Schema 版本。
  - 分区：按 device_sn + component 计算分区键（保持同设备局部顺序）。
  - 限流：按设备/租户的令牌桶（防过载，§16.3）。

失败语义（对齐 §13.4 / §9.1）：身份缺失、认证不过、超限、编码非法 → **失败关闭**，
返回统一错误码，绝不"降级为无鉴权"。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from ..errors import ToolErrorCode, err
from .events import RawLogEnvelope

# 认领的编码白名单（未知编码 → 拒绝）
_ALLOWED_ENCODINGS = {"utf-8", "utf8", "ascii", "hex", "base64"}
_MAX_PAYLOAD_BYTES = 64 * 1024          # 单条载荷上限（超限拒绝，§16.3）
_REQUIRED_FIELDS = ("event_id", "occurred_at", "received_at", "sequence",
                    "vendor", "model", "firmware")


@dataclass(frozen=True)
class AuthenticatedCaller:
    """认证通过后的可信身份（网关注入；调用方无法伪造）。"""
    tenant_id: str
    device_sn: str
    principal: str = "gateway"


@dataclass
class _Bucket:
    tokens: float
    last: float


@dataclass
class GatewayStats:
    accepted: int = 0
    rejected: int = 0
    rate_limited: int = 0
    by_reason: Dict[str, int] = field(default_factory=dict)

    def _bump(self, reason: str) -> None:
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1


class TelemetryGateway:
    """接入网关。认证桩 + 校验 + 分区 + 限流。

    rate_per_sec：每设备每秒允许的条数（令牌桶容量 = burst）。
    """

    def __init__(self, *, rate_per_sec: float = 200.0, burst: int = 400):
        self.rate_per_sec = rate_per_sec
        self.burst = burst
        self._buckets: Dict[str, _Bucket] = {}
        self.stats = GatewayStats()
        self._clock = time.monotonic

    # -- 认证（桩）：真实部署由网关注入；这里演示"正文身份一律不作数" --
    def authenticate(self, auth: Optional[Dict[str, Any]]) -> Optional[AuthenticatedCaller]:
        if not auth:
            return None
        tenant = auth.get("tenant_id")
        device = auth.get("device_sn")
        if not tenant or not device:
            return None
        return AuthenticatedCaller(tenant_id=str(tenant), device_sn=str(device),
                                   principal=str(auth.get("principal", "gateway")))

    # -- 限流 --
    def _allow(self, key: str) -> bool:
        now = self._clock()
        b = self._buckets.get(key)
        if b is None:
            self._buckets[key] = _Bucket(tokens=self.burst - 1, last=now)
            return True
        # 补令牌
        b.tokens = min(self.burst, b.tokens + (now - b.last) * self.rate_per_sec)
        b.last = now
        if b.tokens < 1:
            return False
        b.tokens -= 1
        return True

    # -- 分区键 --
    @staticmethod
    def partition_key(device_sn: str, component: str) -> str:
        return f"{device_sn}|{component}"

    def ingest(self, raw: Dict[str, Any], auth: Optional[Dict[str, Any]]
               ) -> Tuple[bool, Any]:
        """接收一条原始日志。返回 (ok, event_or_error)。

        注意：raw 里**即使带 tenant_id/device_sn，也一律被认证身份覆盖**。
        """
        caller = self.authenticate(auth)
        if caller is None:
            self.stats.rejected += 1
            self.stats._bump("auth")
            return False, err(ToolErrorCode.DENIED, "认证失败或缺少可信身份")

        if not isinstance(raw, dict):
            self.stats.rejected += 1
            self.stats._bump("not_a_dict")
            return False, err(ToolErrorCode.INVALID_INPUT, "载荷不是对象")

        # 必需字段
        missing = [f for f in _REQUIRED_FIELDS if raw.get(f) in (None, "")]
        if missing:
            self.stats.rejected += 1
            self.stats._bump("missing_fields")
            return False, err(ToolErrorCode.INVALID_INPUT, f"缺必需字段: {missing}")

        # 编码白名单
        enc = str(raw.get("encoding", "utf-8")).lower()
        if enc not in _ALLOWED_ENCODINGS:
            self.stats.rejected += 1
            self.stats._bump("bad_encoding")
            return False, err(ToolErrorCode.INVALID_INPUT, f"不支持的编码: {enc}")

        # 大小限制
        inline = raw.get("payload_inline")
        if inline is not None:
            size = len(inline.encode("utf-8", errors="replace"))
            if size > _MAX_PAYLOAD_BYTES:
                self.stats.rejected += 1
                self.stats._bump("too_large")
                return False, err(ToolErrorCode.INVALID_INPUT,
                                  f"载荷过大: {size}B > {_MAX_PAYLOAD_BYTES}B")

        # 限流
        if not self._allow(caller.device_sn):
            self.stats.rate_limited += 1
            self.stats.rejected += 1
            self.stats._bump("rate_limited")
            return False, err(ToolErrorCode.RATE_LIMITED, f"设备 {caller.device_sn} 超限")

        # 身份以认证上下文为准（正文里的身份被丢弃）
        env = RawLogEnvelope(
            event_id=str(raw["event_id"]),
            tenant_id=caller.tenant_id,                # ← 注入，不读正文
            device_sn=caller.device_sn,                # ← 注入，不读正文
            occurred_at=str(raw["occurred_at"]),
            received_at=str(raw["received_at"]),
            sequence=int(raw["sequence"]),
            vendor=str(raw["vendor"]),
            model=str(raw["model"]),
            firmware=str(raw["firmware"]),
            encoding=enc,
            payload_ref=str(raw.get("payload_ref", "")),
            payload_digest=str(raw.get("payload_digest", "")),
            protocol_version=str(raw.get("protocol_version", "")),
            component_hint=str(raw.get("component_hint", "")),
            payload_inline=inline,
        )
        self.stats.accepted += 1
        return True, env
