# -*- coding: utf-8 -*-
"""P2 标准事件模型（对齐架构 §12.1 / §12.2）。

- RawLogEnvelope：网关接入的原始日志信封。身份字段（tenant_id/device_sn）**只由网关注入**，
  日志正文里的同名字段不被信任。
- NormalizedTelemetryEvent：解析/标准化后的遥测事件，是 Window/Detector 的输入。

命名与字段以 schemas/*.json 的正式契约为准（P0-1）。本模块是它的 Python 实现。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RawLogEnvelope:
    """原始日志信封（§12.1）。"""
    event_id: str
    tenant_id: str                  # 网关注入，不读正文
    device_sn: str                  # 网关注入，不读正文
    occurred_at: str                # 设备事件时间（ISO8601）
    received_at: str                # 网关接收时间
    sequence: int                   # 设备单调序列号
    vendor: str
    model: str
    firmware: str
    encoding: str = "utf-8"
    payload_ref: str = ""           # 受控引用（正文外置）
    payload_digest: str = ""        # 正文 sha256
    protocol_version: str = ""
    component_hint: str = ""        # 非权威提示
    payload_inline: Optional[str] = None   # 小载荷可直接内联（大载荷走 payload_ref）

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class NormalizedTelemetryEvent:
    """规范化遥测事件（§12.2）。"""
    event_id: str
    tenant_id: str
    device_sn: str
    occurred_at: str
    component_type: str
    component_instance: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    status_codes: List[str] = field(default_factory=list)
    operating_condition: str = ""
    parse_confidence: float = 1.0
    missing_fields: List[str] = field(default_factory=list)
    clock_skew_ms: int = 0
    operator_id: str = ""
    operator_version: str = ""
    binding_revision: str = ""
    raw_log_ref: str = ""

    @property
    def component_ref(self) -> str:
        return f"{self.component_type}.{self.component_instance}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
