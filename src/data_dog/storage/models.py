# -*- coding: utf-8 -*-
"""ORM 表定义（SQLAlchemy 2.0 声明式）。

覆盖项目里需要"长期存储、重启不丢"的核心对象：
  telemetry_events   规范化遥测事件（P2 产出）
  candidates         候选异常（P2 产出，对齐 §12.3）
  reports            调查报告（Agent 产出，对齐 §12.4）
  alerts             正式事件（P3 产出，对齐 §12.5）
  alert_outbox       上报 outbox（幂等投递，要求持久）
  alert_feedback     人工/维修反馈

设计取舍：
  - 复杂/多态字段（metrics、hypotheses、evidence…）用 **JSON**（PostgreSQL 为 JSONB，
    SQLite 为 TEXT 存 JSON），避免过早拆表；需要查询的**关键字段**单独建列 + 索引。
  - 用 UUID 字符串主键（跨库一致，PostgreSQL/SQLite 都支持）。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from sqlalchemy import Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from .db import Base


def _now() -> float:
    return time.time()


class TelemetryEventRow(Base):
    __tablename__ = "telemetry_events"

    # tenant_id 必须进主键：event_id 由原始日志信封给出，跨租户可能重名，
    # 若只做普通列，两个租户同 event_id 会撞同一 PK、后者静默覆盖前者。
    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    device_sn: Mapped[str] = mapped_column(String(80), index=True)
    occurred_at: Mapped[str] = mapped_column(String(40))
    component_ref: Mapped[str] = mapped_column(String(120), index=True)
    metrics: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    status_codes: Mapped[List[str]] = mapped_column(JSON, default=list)
    operating_condition: Mapped[str] = mapped_column(String(40), default="")
    operator_id: Mapped[str] = mapped_column(String(120), default="")
    operator_version: Mapped[str] = mapped_column(String(40), default="")
    binding_revision: Mapped[str] = mapped_column(String(120), default="")
    raw_log_ref: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class CandidateRow(Base):
    __tablename__ = "candidates"

    # tenant_id 进主键：candidate_id = cand_<sha256(device_sn|component|signal#seq)>，
    # 不含租户 → 两租户同 device_sn 会生成相同 id，只加列+过滤会被静默覆盖。
    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True, index=True)
    candidate_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    device_sn: Mapped[str] = mapped_column(String(80), index=True)
    component_ref: Mapped[str] = mapped_column(String(120), index=True)
    signal: Mapped[str] = mapped_column(String(80), index=True)
    severity_hint: Mapped[str] = mapped_column(String(20), default="info")
    window_start: Mapped[str] = mapped_column(String(40))
    window_end: Mapped[str] = mapped_column(String(40))
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    rule_matches: Mapped[List[str]] = mapped_column(JSON, default=list)
    anomaly_score: Mapped[float] = mapped_column(Float, default=0.0)
    evidence: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    evidence_refs: Mapped[List[str]] = mapped_column(JSON, default=list)
    detector_provenance: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[float] = mapped_column(Float, default=_now)

    __table_args__ = (
        Index("ix_cand_dev_comp", "device_sn", "component_ref"),
    )


class ReportRow(Base):
    __tablename__ = "reports"

    # tenant_id 进主键：investigation_id = inv_<candidate_id>，跨租户同名同 id。
    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True, index=True)
    investigation_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    disposition: Mapped[str] = mapped_column(String(30), index=True)
    severity: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    event_type: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    candidate_refs: Mapped[List[str]] = mapped_column(JSON, default=list)
    hypotheses: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    missing_evidence: Mapped[List[str]] = mapped_column(JSON, default=list)
    recommended_actions: Mapped[List[str]] = mapped_column(JSON, default=list)
    correlation_group_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    model_provenance: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class AlertRow(Base):
    __tablename__ = "alerts"

    # tenant_id 进主键：event_id = alert_<sha256(device_ref:event_type:episode)>，
    # 跨租户同设备号会生成相同 id。
    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True, index=True)
    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), index=True)
    device_ref: Mapped[str] = mapped_column(String(120), index=True)
    component_ref: Mapped[str] = mapped_column(String(120))
    event_type: Mapped[str] = mapped_column(String(120), index=True)
    state: Mapped[str] = mapped_column(String(30), index=True)
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    detected_at: Mapped[str] = mapped_column(String(40), default="")
    event_revision: Mapped[int] = mapped_column(Integer, default=1)
    summary: Mapped[str] = mapped_column(Text, default="")
    recommended_actions: Mapped[List[str]] = mapped_column(JSON, default=list)
    investigation_id: Mapped[str] = mapped_column(String(120), default="")
    policy_version: Mapped[str] = mapped_column(String(40), default="")
    history: Mapped[List[Dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now, onupdate=_now)


class AlertOutboxRow(Base):
    __tablename__ = "alert_outbox"

    # 幂等去重键：同 (幂等键, revision) 唯一 → 用复合主键。
    # tenant_id 进主键：幂等键由设备/事件派生，跨租户可能重名。
    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    receipt: Mapped[str] = mapped_column(String(120), default="")
    last_error: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class AlertFeedbackRow(Base):
    __tablename__ = "alert_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 反馈保留代理主键（一条事件可有多条反馈），租户只作普通列 + 索引。
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    event_id: Mapped[str] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=_now)
