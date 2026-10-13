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
  audit_log          **安全审计**：谁在何时对什么做了什么、结果如何（A1）

设计取舍：
  - 复杂/多态字段（metrics、hypotheses、evidence…）用 **JSON**（PostgreSQL 为 JSONB，
    SQLite 为 TEXT 存 JSON），避免过早拆表；需要查询的**关键字段**单独建列 + 索引。
  - 用 UUID 字符串主键（跨库一致，PostgreSQL/SQLite 都支持）。
"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import Float, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from .db import Base


def _now() -> float:
    return time.time()


# 复杂/多态字段的列类型：**PostgreSQL 上必须是 JSONB**（与模块 docstring 的声明一致，
# 并由 tests/test_project_meta.py 的 `test_json_columns_are_jsonb_on_postgres` 钉住）。
# SQLAlchemy 的通用 JSON 在 PG 上落成 `json`（不是 jsonb）：不能建 GIN 索引、不去重、取值更慢。
# "文档说 JSONB、实际是 json" 正是本仓最忌讳的**声明 ≠ 事实**——所以这里用带方言变体的类型，
# 既有库由迁移 f2a3b4c5d6e7 负责 ALTER。
JsonType = JSON().with_variant(JSONB(), "postgresql")


class TelemetryEventRow(Base):
    __tablename__ = "telemetry_events"

    # tenant_id 必须进主键：event_id 由原始日志信封给出，跨租户可能重名，
    # 若只做普通列，两个租户同 event_id 会撞同一 PK、后者静默覆盖前者。
    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    device_sn: Mapped[str] = mapped_column(String(80), index=True)
    occurred_at: Mapped[str] = mapped_column(String(40))
    component_ref: Mapped[str] = mapped_column(String(120), index=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    status_codes: Mapped[list[str]] = mapped_column(JsonType, default=list)
    operating_condition: Mapped[str] = mapped_column(String(40), default="")
    operator_id: Mapped[str] = mapped_column(String(120), default="")
    operator_version: Mapped[str] = mapped_column(String(40), default="")
    binding_revision: Mapped[str] = mapped_column(String(120), default="")
    raw_log_ref: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[float] = mapped_column(Float, index=True, default=_now)

    # 租户维度的分页/保留查询走"按 created_at 排序"，单列索引在 PG 上用不上排序：
    # (tenant_id, created_at) 才能既过滤又排序（保留清理的 DELETE ... WHERE tenant < cutoff 同理）。
    __table_args__ = (
        Index("ix_telemetry_tenant_created", "tenant_id", "created_at"),
    )


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
    rule_matches: Mapped[list[str]] = mapped_column(JsonType, default=list)
    anomaly_score: Mapped[float] = mapped_column(Float, default=0.0)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list)
    evidence_refs: Mapped[list[str]] = mapped_column(JsonType, default=list)
    detector_provenance: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[float] = mapped_column(Float, index=True, default=_now)

    __table_args__ = (
        Index("ix_cand_dev_comp", "device_sn", "component_ref"),
        # 控制台/保留清理的常用路径：tenant 过滤 + created_at 排序（列尾带上 tiebreaker）
        Index("ix_cand_tenant_created", "tenant_id", "created_at", "candidate_id"),
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
    candidate_refs: Mapped[list[str]] = mapped_column(JsonType, default=list)
    hypotheses: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list)
    counter_evidence_refs: Mapped[list[str]] = mapped_column(JsonType, default=list)
    missing_evidence: Mapped[list[str]] = mapped_column(JsonType, default=list)
    risk_window: Mapped[str | None] = mapped_column(String(120), nullable=True)
    recommended_actions: Mapped[list[str]] = mapped_column(JsonType, default=list)
    correlation_group_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    linked_candidate_refs: Mapped[list[str]] = mapped_column(JsonType, default=list)
    model_provenance: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[float] = mapped_column(Float, index=True, default=_now)

    __table_args__ = (
        Index("ix_reports_tenant_created", "tenant_id", "created_at", "investigation_id"),
    )


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
    recommended_actions: Mapped[list[str]] = mapped_column(JsonType, default=list)
    investigation_id: Mapped[str] = mapped_column(String(120), default="")
    policy_version: Mapped[str] = mapped_column(String(40), default="")
    history: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list)
    created_at: Mapped[float] = mapped_column(Float, index=True, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now, onupdate=_now)

    __table_args__ = (
        Index("ix_alerts_tenant_created", "tenant_id", "created_at", "event_id"),
    )


class AlertOutboxRow(Base):
    __tablename__ = "alert_outbox"

    # 幂等去重键：同 (幂等键, revision) 唯一 → 用复合主键。
    # tenant_id 进主键：幂等键由设备/事件派生，跨租户可能重名。
    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    receipt: Mapped[str] = mapped_column(String(120), default="")
    last_error: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[float] = mapped_column(Float, index=True, default=_now)


class AlertFeedbackRow(Base):
    __tablename__ = "alert_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 反馈保留代理主键（一条事件可有多条反馈），租户只作普通列 + 索引。
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    event_id: Mapped[str] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, index=True, default=_now)


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
    raw: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
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
    value: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
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
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
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
    finished_at: Mapped[float | None] = mapped_column(Float, index=True, nullable=True)

    # /v1/investigations/jobs 是前端轮询最频繁的列表：tenant 过滤 + submitted_at 排序。
    __table_args__ = (
        Index("ix_job_tenant_submitted", "tenant_id", "submitted_at", "job_id"),
    )


class InvestigationRunLogRow(Base):
    """一次调查的**运行日志**（ReAct 轨迹 + 失败上下文）——给"事后调出来排查"用。

    为什么单独一张表，而不是往 reports 加一列
    ----------------------------------------
    - **失败的调查没有 report**，而"为什么失败"恰恰是最需要排查的那一类——把日志挂在
      报告上等于"越需要日志的情况越没有日志"。
    - 同步 / 异步两条路径都要落：异步 worker 此前**直接丢弃** trace（只在同步 HTTP 响应里
      返回给当时守着的人），"事后调出"因此做不到。

    `run_id` 的语义：异步调查 = `job_id`；同步调查 = `investigation_id`（报告 id）。
    两种 id 在各自路径内唯一、跨租户可能重名 → `tenant_id` 进主键（与其余租户表一致）。

    `steps` / `error_detail` 落库前**已脱敏并按大小裁剪**（见 service/runtime.py 的
    `_run_log_payload`）：运行日志要能给人看，但不能把密钥/明文设备号带出去。
    保留策略见 `retention.py`（默认 90 天；`DD_RETENTION_RUN_LOG_DAYS=0` 即永久保留）。
    """
    __tablename__ = "investigation_run_log"

    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True, index=True)
    run_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(80), index=True, default="")
    kind: Mapped[str] = mapped_column(String(16), default="sync")            # sync | async
    status: Mapped[str] = mapped_column(String(20), index=True, default="")  # succeeded/failed/cancelled
    report_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_detail: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    step_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list)
    usage: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[float] = mapped_column(Float, index=True, default=_now)


class OperatorRegistryRow(Base):
    """算子注册表（P4-2 控制面）：每个 (operator_id, version) 一行。

    这是算子工厂从"demo"变"产品"的落点——版本化注册、签名、灰度、回滚的**持久化**。

    - `state`：复用 `lifecycle.STATES`（Registered/Shadow/Canary/Active/…）。
    - `dsl_hash`：`sha256(dsl_source)`，签名绑定的内容哈希（改一字节即验签失败）。
    - `signature`：`{alg, key_id, sig, signed_at}`，验签通过方可注册（失败关闭）。
    - `rollout`：`{stage, percent, shadow_of}`，canary 流量路由的依据。
    - `metrics_snapshot`：最近一次门控指标（自动回滚看门狗读它判是否越限）。

    `tenant_id` 进主键：与其余租户表一致（跨租户读 404）；RLS 迁移同步加策略。
    """
    __tablename__ = "operator_registry"

    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True, index=True)
    operator_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    version: Mapped[str] = mapped_column(String(40), primary_key=True)
    state: Mapped[str] = mapped_column(String(30), index=True, default="Registered")
    dsl_source: Mapped[str] = mapped_column(Text, default="")
    dsl_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    signature: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    rollout: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    metrics_snapshot: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[float] = mapped_column(Float, default=_now)
    updated_at: Mapped[float] = mapped_column(Float, default=_now, onupdate=_now)


class OperatorRolloutEventRow(Base):
    """算子状态转移台账（P4-2，审计）。

    每次 `transition/rollback/quarantine/canary` 落一行——回答"这个版本什么时候、
    因何、由谁进的 Canary/被回滚"。与告警的 `history` JSON 不同，这里是**独立表**：
    审计需按时间/操作人跨算子查询，JSON 数组做不到。
    """
    __tablename__ = "operator_rollout_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 事件保留代理主键（一个版本可有多条转移），租户只作普通列 + 索引。
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    operator_id: Mapped[str] = mapped_column(String(120), index=True)
    version: Mapped[str] = mapped_column(String(40), index=True)
    from_state: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_state: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(120), default="")
    at: Mapped[float] = mapped_column(Float, default=_now)


class OncallRotaRow(Base):
    """值班表（P4-3）：谁在什么时段值班，按领域与级别。

    一条 = 一个 (领域, 级别, 时间窗) 的排班。查"当前谁值班"在应用层做（`oncall.py`）：
    取 `start_at <= now < end_at` 且领域匹配的**级别最低**条目（一线优先）。

    `rota_id = hash(domain|level|start_at)` 确定性派生：同一排班反复 PUT 收敛到同一
    行。`tenant_id` 进主键（与其余租户表一致）；RLS 迁移同步加策略。
    """
    __tablename__ = "oncall_rota"

    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True, index=True)
    rota_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    domain: Mapped[str] = mapped_column(String(120), index=True, default="*")
    level: Mapped[int] = mapped_column(Integer, default=1)
    assignee: Mapped[str] = mapped_column(String(120), default="")
    start_at: Mapped[float] = mapped_column(Float, default=_now)
    end_at: Mapped[float] = mapped_column(Float, default=_now)
    channel: Mapped[str] = mapped_column(String(40), default="webhook")
    target: Mapped[str] = mapped_column(String(400), default="")
    created_at: Mapped[float] = mapped_column(Float, default=_now)


class AlertDeliveryRow(Base):
    """告警投递台账（P4-3）：每次投递尝试一行，兼作**升级计时**依据。

    幂等：`delivery_id = hash(event_id, level)` —— 同一告警同一级只投一次（重复触发
    是 UPSERT 更新而非新增行）。

    - `status`：pending（已发待 ack）/ acked（有人确认）/ escalated（超 SLA 已升级）
      / failed（到最高级仍无 ack，升级链尽头）。
    - `ack_deadline`：超此未被 ack → 升级到 `level+1`（写新行）。
    - `attempts` / `last_error`：投递失败的审计（重投由升级引擎按 SLA 节奏重发）。
    """
    __tablename__ = "alert_delivery"

    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True, index=True)
    delivery_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(80), index=True)
    level: Mapped[int] = mapped_column(Integer, default=1)
    domain: Mapped[str] = mapped_column(String(120), default="*")
    channel: Mapped[str] = mapped_column(String(40), default="webhook")
    target: Mapped[str] = mapped_column(String(400), default="")
    assignee: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(20), index=True, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    acked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    ack_deadline: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, index=True, default=_now)


class AuditLogRow(Base):
    """**安全审计**记录（A1）：谁在何时对什么做了什么、结果如何。

    与其余"业务台账"的区别——它是给**合规与事故复盘**看的（`docs/slo.md` §6 要求
    "审计记录保留 ≥ 1 年"，此前 `src/` 里零实现）：

    - **只增不改**（没有更新路径）：审计记录被改写就失去意义。`audit_id` 每条唯一
      （`aud_<uuid>`），**不是**确定性幂等 id——两次相同事件就是两条事实。
    - **拒绝也要留痕**：认证/授权**失败**本身就是最值得看的事件（谁在试探哪个租户），
      所以 `outcome` 有 `denied` 这一档，而不是"只记成功"。
    - `key_id`：**哪把钥匙**做的（不是密钥本身）——为密钥轮换/吊销（②）预留，
      也让"某把钥匙在什么时候开始异常"可查。
    - `request_id` / `trace_id`：与该请求的日志、链路对齐（`core/logging`、`core/tracing`）。
    - `tenant_id` 放主键（与其余租户表一致）。**无租户上下文的系统事件**（未带
      `X-Tenant-Id` 的拒绝、运维面操作）记在保留租户 `-` 下（见 `audit.SYSTEM_TENANT`），
      只有系统扫描/运维查询能看到。
    """
    __tablename__ = "audit_log"

    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True, index=True)
    audit_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    ts: Mapped[float] = mapped_column(Float, index=True, default=_now)
    # 主体：key_id / principal / "anonymous"（不记密钥本身，只记它是哪一把）
    actor: Mapped[str] = mapped_column(String(160), default="")
    # 动作：auth.denied / ops.access / tenant.purge / key.rotate …
    action: Mapped[str] = mapped_column(String(80), index=True, default="")
    target: Mapped[str] = mapped_column(String(200), default="")
    # 结果：allowed / denied / error
    outcome: Mapped[str] = mapped_column(String(20), index=True, default="")
    reason: Mapped[str] = mapped_column(String(200), default="")
    request_id: Mapped[str] = mapped_column(String(80), default="")
    trace_id: Mapped[str] = mapped_column(String(80), default="")
    key_id: Mapped[str] = mapped_column(String(40), default="")
    source_ip: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
