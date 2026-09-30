"""ORM 表定义（SQLAlchemy 2.0 声明式）。

覆盖项目里需要"长期存储、重启不丢"的核心对象：
  telemetry_events   规范化遥测事件（P2 产出）
  candidates         候选异常（P2 产出，对齐 §12.3）
  reports            调查报告（Agent 产出，对齐 §12.4）
  alerts             正式事件（P3 产出，对齐 §12.5）
  alert_outbox       上报 outbox（幂等投递，要求持久）
  alert_feedback     人工/维修反馈
  knowledge_documents/chunks  用户导入的厂商文档（A7）
  investigation_job  异步调查任务的执行状态（G1）
  state_checkpoint   共享状态的周期快照（G3）

设计取舍：
  - 复杂/多态字段（metrics、hypotheses、evidence…）用 **JSON**（PostgreSQL 为 JSONB，
    SQLite 为 TEXT 存 JSON），避免过早拆表；需要查询的**关键字段**单独建列 + 索引。
  - 用 UUID 字符串主键（跨库一致，PostgreSQL/SQLite 都支持）。
"""
from __future__ import annotations

import time
from typing import Any

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
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
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
    rule_matches: Mapped[list[str]] = mapped_column(JSON, default=list)
    anomaly_score: Mapped[float] = mapped_column(Float, default=0.0)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
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
    severity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    event_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    candidate_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    hypotheses: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    counter_evidence_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    risk_window: Mapped[str | None] = mapped_column(String(120), nullable=True)
    recommended_actions: Mapped[list[str]] = mapped_column(JSON, default=list)
    correlation_group_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    linked_candidate_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    model_provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
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
    recommended_actions: Mapped[list[str]] = mapped_column(JSON, default=list)
    investigation_id: Mapped[str] = mapped_column(String(120), default="")
    policy_version: Mapped[str] = mapped_column(String(40), default="")
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now, onupdate=_now)


class AlertOutboxRow(Base):
    __tablename__ = "alert_outbox"

    # 幂等去重键：同 (幂等键, revision) 唯一 → 用复合主键。
    # tenant_id 进主键：幂等键由设备/事件派生，跨租户可能重名。
    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
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


class KnowledgeDocumentRow(Base):
    """用户导入的厂商文档（A7 用户通道）。**租户隔离**：每租户各存各的。

    内容寻址：document_id = kdoc_<sha256[:12]>，同内容重复导入 → 同 id（幂等）。
    与内置 chroma 语料（全局一份公开资料）不同，用户文档按租户隔离——别人的
    厂商原件不应被你的租户检索到。
    """
    __tablename__ = "knowledge_documents"

    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True, index=True)
    document_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    source_name: Mapped[str] = mapped_column(String(200))
    content_sha256: Mapped[str] = mapped_column(String(64))
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class ChangeRecordRow(Base):
    """变更记录（G7：推送式变更管理源）。

    变更系统（CMDB/发布系统）调 `POST /v1/changes/ingest` 把"变了什么"落此表，
    Agent 的 `query_recent_changes` 查库并**真按 since 过滤**。与内置假表
    `RECENT_CHANGES` 不同：这是**真实接入**的持久化载体，租户隔离。

    主键 (tenant_id, change_id)：change_id 由来源系统给出，跨租户可能重名。
    `raw` 存来源特有字段（what/note/relevance…），对 Agent 返回时展开，
    保持与内置表一致的形状。
    """
    __tablename__ = "change_records"

    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True, index=True)
    change_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), default="")
    target: Mapped[str] = mapped_column(String(120), index=True, default="")
    applied_at: Mapped[str] = mapped_column(String(40), index=True, default="")
    from_version: Mapped[str] = mapped_column(String(80), default="")
    to_version: Mapped[str] = mapped_column(String(80), default="")
    actor: Mapped[str] = mapped_column(String(120), default="")
    source: Mapped[str] = mapped_column(String(80), default="")
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class StateKvRow(Base):
    """跨副本共享状态（限流桶 / 候选分组）。

    水平扩展的落点：有状态算子不能只活在进程内。此表是所有副本共享的
    状态载体，`mutate` 以 `SELECT ... FOR UPDATE` 行锁做原子 read-modify-write。

    为什么不复用 tenant 复合主键：状态 key 已内含租户/设备维度（如
    `rate:<device_sn>`），且需跨租户统一按 key 老化，故用单一 TEXT 主键。
    """
    __tablename__ = "state_kv"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[float] = mapped_column(Float, default=_now)


class StateCheckpointRow(Base):
    """共享状态的**周期快照**（G3：checkpoint / 恢复）。

    状态已外置（`state_kv`）可跨副本共享，但**重启/淘汰后无"上次算到哪"**。
    每次快照导出一份状态词典（按 prefix 过滤）存于此表，供恢复与对账。

    语义边界（诚实）：这**不是**流处理器的精确一次 checkpoint——快照是**周期**的，
    两次快照之间重启会丢这段增量（RPO = 间隔）。它是"状态词典的周期快照"。

    `payload` 存状态词典（可含租户维度），故为**敏感数据**——访问控制/加密属运营职责。
    主键 checkpoint_id = chk_<ts>_<hash>（时间有序，便于取最新 / 按保留数清理）。
    """
    __tablename__ = "state_checkpoint"

    checkpoint_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    prefix: Mapped[str] = mapped_column(String(300), default="")
    n_keys: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=_now, index=True)


class KnowledgeChunkRow(Base):
    """导入文档的切片。主键 (tenant_id, chunk_id)，chunk_id 由文档+序号派生。"""
    __tablename__ = "knowledge_chunks"

    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True, index=True)
    chunk_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(80), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class InvestigationJobRow(Base):
    """异步调查任务的**执行状态**（G1）。

    与 `reports`（调查的**产物**，长期保留）分离：job 是短命的执行记录，
    完成后 `report_id` 指向落库报告。这样 `POST /v1/investigations` 能立即返回
    一个可轮询的句柄，不再把 worker 绑在一次可能十几秒的 LLM 调用上。

    状态机：pending → running → {succeeded, failed, cancelled}；pending 可直接被
    取消（cancelled）。终态不可再变（仓储层校验，避免取消与完成互相覆盖）。

    `tenant_id` 进主键：job_id 由 租户/候选/随机数 派生，仍按复合主键隔离，
    与其余租户表一致（跨租户读 404）。RLS 对**本表同样启用**——见迁移注释。
    """
    __tablename__ = "investigation_job"

    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True, index=True)
    job_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True, default="pending")
    report_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    worker_id: Mapped[str] = mapped_column(String(120), default="")
    submitted_at: Mapped[float] = mapped_column(Float, default=_now)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
