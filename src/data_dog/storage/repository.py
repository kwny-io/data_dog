"""仓储层：领域对象 ↔ 持久化行的转换与读写。

供 P2（候选）与 P3（Alert / Outbox / 反馈）调用，实现"重启不丢"。
所有写入都是按主键的**幂等 upsert**：重复保存不报错、不产生重复记录。

安全说明：本模块**不做任何语句字符串拼接**，全部通过 ORM 的
`session.get` / `session.query` / 表达式构造器完成；值以**绑定参数**传递，
因此不存在注入面。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models as M


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return dict(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return dict(vars(obj))


def _row_dict(r: Any) -> dict[str, Any]:
    """ORM 行 → 普通 dict（只取真实列）。"""
    return {c.name: getattr(r, c.name) for c in r.__table__.columns}


def _upsert(session: Session, model: Any, pk: Any, pk_kwargs: dict[str, Any],
            apply_fn: Callable[[Any], None]) -> bool:
    """并发安全的按主键 upsert。返回 True=新建，False=更新。

    为什么不能只用 get-then-add：并发的两个请求可能同时 `get` 到 None，
    于是各自 `INSERT` 同一主键 —— 后者在 flush 时抛 IntegrityError。
    （实测 8 并发线程写同一 id，2 个抛错。）
    在 HTTP 服务里这会被兜底成 `persist_failed`，把一次**有效写入**误报为失败。

    做法：先试 get；没有则把 INSERT 包在 SAVEPOINT 里，若撞唯一约束
    （说明并发下别的事务刚插入了同一行）则回滚该 SAVEPOINT 并改为更新。
    SAVEPOINT 保证只回滚这一行，不牵连外层事务已做的其它写入。
    """
    row = session.get(model, pk)
    if row is not None:
        apply_fn(row)
        session.flush()
        return False

    # 新建：**先填好字段再 add**，否则 add 触发的 autoflush 会 INSERT 一个
    # 字段全空的骨架行，直接撞 NOT NULL 约束。
    row = model(**pk_kwargs)
    apply_fn(row)
    try:
        with session.begin_nested():          # SAVEPOINT
            session.add(row)
            session.flush()
        return True
    except IntegrityError:
        # 并发竞态：别的事务抢先插入了同一主键。SAVEPOINT 已回滚该 INSERT，
        # 重新取回已存在的行并改为更新。
        if row in session:
            session.expunge(row)
        existing = session.get(model, pk)
        if existing is None:
            raise                             # 真异常（非竞态），照常抛出
        apply_fn(existing)
        session.flush()
        return False


# ---------------------------------------------------------------- 候选

def _apply_candidate(row: Any, d: dict[str, Any]) -> None:
    row.device_sn = d.get("device_sn", "")
    row.component_ref = d.get("component_ref", "")
    row.signal = d.get("signal", "")
    row.severity_hint = d.get("severity_hint", "info")
    row.window_start = d.get("window_start", "")
    row.window_end = d.get("window_end", "")
    row.sample_count = int(d.get("sample_count", 0))
    row.rule_matches = list(d.get("rule_matches", []))
    row.anomaly_score = float(d.get("anomaly_score", 0.0))
    row.evidence = [e if isinstance(e, dict) else _as_dict(e)
                    for e in d.get("evidence", [])]
    row.evidence_refs = list(d.get("evidence_refs", []))
    row.detector_provenance = d.get("detector_provenance", "")


def save_candidates(session: Session, candidates: list[Any], *, tenant_id: str) -> int:
    """保存候选（幂等：已存在则更新）。返回写入条数。并发安全。

    主键是 (tenant_id, candidate_id)：candidate_id 由设备/部件/信号派生、
    不含租户，故必须用复合主键，否则跨租户同名 id 会互相覆盖。
    """
    n = 0
    for c in candidates:
        d = _as_dict(c)
        cid = d["candidate_id"]

        def apply_fn(row: Any, _d: dict[str, Any] = d) -> None:  # noqa: B023
            _apply_candidate(row, _d)

        _upsert(session, M.CandidateRow, (tenant_id, cid),
                {"tenant_id": tenant_id, "candidate_id": cid}, apply_fn)
        n += 1
    return n


def _page(q: Any, limit: int | None, offset: int) -> list[dict[str, Any]]:
    """分页执行查询。limit=None 表示不分页（保持旧行为，供内部调用）。"""
    if offset:
        q = q.offset(max(0, int(offset)))
    if limit is not None:
        q = q.limit(max(0, int(limit)))
    return [_row_dict(r) for r in q.all()]


def count_rows(session: Session, model: Any, **filters: Any) -> int:
    """按条件计数（用 COUNT(*) 聚合，**不把全表拉进内存**）。

    **不做租户过滤**：这是全局运维指标（/metrics 用），故意跨租户。
    租户作用域的计数请传 `tenant_id=` 作为 filter。
    """
    q = session.query(func.count()).select_from(model)
    for col, val in filters.items():
        q = q.filter(getattr(model, col) == val)
    return int(q.scalar() or 0)


def list_candidates(session: Session, *, tenant_id: str,
                    device_sn: str | None = None,
                    component_ref: str | None = None,
                    limit: int | None = None,
                    offset: int = 0) -> list[dict[str, Any]]:
    q = session.query(M.CandidateRow).filter(
        M.CandidateRow.tenant_id == tenant_id)
    if device_sn is not None:
        q = q.filter(M.CandidateRow.device_sn == device_sn)
    if component_ref is not None:
        q = q.filter(M.CandidateRow.component_ref == component_ref)
    # created_at 可能重复 → 追加主键做稳定排序，否则分页会漏/重
    q = q.order_by(M.CandidateRow.created_at, M.CandidateRow.candidate_id)
    return _page(q, limit, offset)


def get_candidate(session: Session, candidate_id: str, *,
                  tenant_id: str) -> dict[str, Any] | None:
    """取单个候选。跨租户（或不存在）→ None，供路由返回 404、不泄露存在性。"""
    r = session.get(M.CandidateRow, (tenant_id, candidate_id))
    return _row_dict(r) if r else None


# ---------------------------------------------------------------- 调查报告

def save_report(session: Session, report: Any, *, tenant_id: str) -> None:
    d = _as_dict(report)
    rid = d["investigation_id"]
    pk = (tenant_id, rid)
    row = session.get(M.ReportRow, pk) or M.ReportRow(
        tenant_id=tenant_id, investigation_id=rid)
    session.add(row)
    row.disposition = d.get("disposition", "")
    row.severity = d.get("severity")
    row.confidence = float(d.get("confidence", 0.0))
    row.event_type = d.get("event_type")
    row.summary = d.get("summary", "")
    row.candidate_refs = list(d.get("candidate_refs", []))
    row.hypotheses = [_as_dict(h) for h in d.get("hypotheses", [])]
    row.counter_evidence_refs = list(d.get("counter_evidence_refs", []) or [])
    row.missing_evidence = list(d.get("missing_evidence", []))
    row.risk_window = d.get("risk_window")
    row.recommended_actions = list(d.get("recommended_actions", []))
    row.correlation_group_id = d.get("correlation_group_id")
    row.linked_candidate_refs = list(d.get("linked_candidate_refs", []) or [])
    row.model_provenance = d.get("model_provenance", {}) or {}
    session.flush()


def get_report(session: Session, investigation_id: str, *,
               tenant_id: str) -> dict[str, Any] | None:
    """取单份报告。跨租户（或不存在）→ None（路由返回 404）。"""
    r = session.get(M.ReportRow, (tenant_id, investigation_id))
    return _row_dict(r) if r else None


def list_reports(session: Session, *, tenant_id: str,
                 limit: int | None = None,
                 offset: int = 0) -> list[dict[str, Any]]:
    q = (session.query(M.ReportRow)
         .filter(M.ReportRow.tenant_id == tenant_id)
         .order_by(M.ReportRow.created_at, M.ReportRow.investigation_id))
    return _page(q, limit, offset)


# ---------------------------------------------------------------- Alert

def save_alert(session: Session, event: Any, *, tenant_id: str) -> None:
    d = _as_dict(event)
    eid = d["event_id"]
    _upsert(session, M.AlertRow, (tenant_id, eid),
            {"tenant_id": tenant_id, "event_id": eid},
            lambda row: _apply_alert(row, d))


def _apply_alert(row: Any, d: dict[str, Any]) -> None:
    row.idempotency_key = d.get("idempotency_key", "")
    row.device_ref = d.get("device_ref", "")
    row.component_ref = d.get("component_ref", "")
    row.event_type = d.get("event_type", "")
    row.state = d.get("state", "detected")
    row.severity = d.get("severity", "warning")
    row.confidence = float(d.get("confidence", 0.0))
    row.detected_at = d.get("detected_at", "")
    row.event_revision = int(d.get("event_revision", 1))
    row.summary = d.get("summary", "")
    row.recommended_actions = list(d.get("recommended_actions", []))
    row.investigation_id = d.get("investigation_id", "")
    row.policy_version = d.get("policy_version", "")
    row.history = list(d.get("history", []))


def get_alert(session: Session, event_id: str, *,
              tenant_id: str) -> dict[str, Any] | None:
    r = session.get(M.AlertRow, (tenant_id, event_id))
    return _row_dict(r) if r else None


def list_alerts(session: Session, *, tenant_id: str,
                limit: int | None = None,
                offset: int = 0) -> list[dict[str, Any]]:
    q = (session.query(M.AlertRow)
         .filter(M.AlertRow.tenant_id == tenant_id)
         .order_by(M.AlertRow.created_at, M.AlertRow.event_id))
    return _page(q, limit, offset)


def count_alerts_by_state(session: Session, *, tenant_id: str) -> dict[str, int]:
    """按 state 分组统计告警数（租户内）。用 GROUP BY 聚合，不拉全表。"""
    q = (session.query(M.AlertRow.state, func.count())
         .filter(M.AlertRow.tenant_id == tenant_id)
         .group_by(M.AlertRow.state))
    return {state: int(n) for state, n in q.all()}


# ---------------------------------------------------------------- Outbox

def upsert_outbox(session: Session, *, tenant_id: str, idempotency_key: str,
                  revision: int, payload: dict[str, Any]) -> bool:
    """写入 outbox 条目。已存在 → 返回 False（幂等，不重复入队）。并发安全。

    主键 (tenant_id, idempotency_key, revision)：幂等键跨租户可能重名。
    """
    pk = (tenant_id, idempotency_key, revision)

    def _apply(row: Any) -> None:
        # 已存在时不覆盖 payload（保留首次写入的语义），仅在竞态分支被调用
        pass

    row = session.get(M.AlertOutboxRow, pk)
    if row is not None:
        return False
    new_row = M.AlertOutboxRow(tenant_id=tenant_id, idempotency_key=idempotency_key,
                               revision=revision, payload=payload, status="pending")
    try:
        with session.begin_nested():          # SAVEPOINT
            session.add(new_row)
            session.flush()
        return True
    except IntegrityError:
        # 并发竞态：别的事务抢先插入了同 (key, revision) → 视为重复，不入队
        if new_row in session:
            session.expunge(new_row)
        return False


def mark_outbox_delivered(session: Session, *, tenant_id: str, idempotency_key: str,
                          revision: int, receipt: str) -> None:
    row = session.get(M.AlertOutboxRow, (tenant_id, idempotency_key, revision))
    if row is not None:
        row.status = "delivered"
        row.receipt = receipt
        session.flush()


def list_outbox(session: Session, *, tenant_id: str,
                status: str | None = None,
                limit: int | None = None,
                offset: int = 0) -> list[dict[str, Any]]:
    q = session.query(M.AlertOutboxRow).filter(
        M.AlertOutboxRow.tenant_id == tenant_id)
    if status is not None:
        q = q.filter(M.AlertOutboxRow.status == status)
    q = q.order_by(M.AlertOutboxRow.created_at, M.AlertOutboxRow.idempotency_key,
                   M.AlertOutboxRow.revision)
    return _page(q, limit, offset)


# ---------------------------------------------------------------- 反馈

def save_feedback(session: Session, *, tenant_id: str, event_id: str, kind: str,
                  note: str = "") -> None:
    session.add(M.AlertFeedbackRow(tenant_id=tenant_id, event_id=event_id,
                                   kind=kind, note=note))
    session.flush()


def list_feedback(session: Session, *, tenant_id: str) -> list[dict[str, Any]]:
    q = (session.query(M.AlertFeedbackRow)
         .filter(M.AlertFeedbackRow.tenant_id == tenant_id)
         .order_by(M.AlertFeedbackRow.created_at))
    return [_row_dict(r) for r in q.all()]


def list_feedback_for_event(session: Session, *, tenant_id: str,
                            event_id: str) -> list[dict[str, Any]]:
    """取某条告警的反馈（租户内），按时间先后。"""
    q = (session.query(M.AlertFeedbackRow)
         .filter(M.AlertFeedbackRow.tenant_id == tenant_id,
                 M.AlertFeedbackRow.event_id == event_id)
         .order_by(M.AlertFeedbackRow.created_at))
    return [_row_dict(r) for r in q.all()]


def list_feedback_with_report(session: Session, *, tenant_id: str
                              ) -> list[dict[str, Any]]:
    """把每条人工反馈与其**告警对应的 Agent 报告裁决**关联起来（租户内）。

    关联链：`alert_feedback.event_id` → `alerts`（取 investigation_id + 当前状态）
    → `reports`（取 Agent 当时的 disposition）。这是"反馈回灌评估"的数据基础：
    有了 (人工 kind, Agent disposition) 的对，才能算一致率/误报率。

    左连接：报告可能尚未落库（如调查失败/告警由别处产生），此时 agent_disposition
    为 None——**如实返回**，由上层归入 unlabeled，不臆造裁决。
    """
    q = (session.query(M.AlertFeedbackRow, M.AlertRow, M.ReportRow)
         .outerjoin(M.AlertRow,
                    (M.AlertRow.tenant_id == M.AlertFeedbackRow.tenant_id)
                    & (M.AlertRow.event_id == M.AlertFeedbackRow.event_id))
         .outerjoin(M.ReportRow,
                    (M.ReportRow.tenant_id == M.AlertFeedbackRow.tenant_id)
                    & (M.ReportRow.investigation_id == M.AlertRow.investigation_id))
         .filter(M.AlertFeedbackRow.tenant_id == tenant_id)
         .order_by(M.AlertFeedbackRow.created_at))
    out: list[dict[str, Any]] = []
    for fb, alert, report in q.all():
        out.append({
            "event_id": fb.event_id,
            "kind": fb.kind,
            "note": fb.note,
            "created_at": fb.created_at,
            "investigation_id": alert.investigation_id if alert else "",
            "alert_state": alert.state if alert else None,
            "agent_disposition": report.disposition if report else None,
            "agent_confidence": report.confidence if report else None,
        })
    return out


def set_alert_state(session: Session, *, tenant_id: str, event_id: str,
                    to_state: str, reason: str) -> dict[str, Any] | None:
    """把告警状态改为 `to_state` 并追加审计历史。**不做合法性校验**——由调用方
    （服务层，持有状态转移表）判断后再调用。返回更新后的行 dict；无此告警返回
    None（跨租户亦不可见）。"""
    row = session.get(M.AlertRow, (tenant_id, event_id))
    if row is None:
        return None
    frm = row.state
    row.state = to_state
    row.event_revision = int(row.event_revision or 1) + 1
    hist = list(row.history or [])
    hist.append({"from": frm, "to": to_state, "reason": reason,
                 "at": M._now(), "revision": row.event_revision})
    row.history = hist
    session.flush()
    return _row_dict(row)


# ---------------------------------------------------------------- 遥测事件

def save_telemetry(session: Session, events: list[Any], *, tenant_id: str) -> int:
    """保存规范化遥测。**租户来自调用方（认证上下文），忽略事件正文里的 tenant_id**
    ——与「身份只在网关注入、正文自称一律丢弃」的规则一致。主键 (tenant_id, event_id)。"""
    n = 0
    for e in events:
        d = _as_dict(e)
        eid = d["event_id"]

        def apply_fn(row: Any, _d: dict[str, Any] = d) -> None:  # noqa: B023
            _apply_telemetry(row, _d)

        _upsert(session, M.TelemetryEventRow, (tenant_id, eid),
                {"tenant_id": tenant_id, "event_id": eid}, apply_fn)
        n += 1
    return n


def _apply_telemetry(row: Any, d: dict[str, Any]) -> None:
    # tenant_id 不在此设置：它来自认证上下文（save_telemetry 的显式入参），
    # 绝不从事件正文取——正文的身份声明是不可信的。
    row.device_sn = d.get("device_sn", "")
    row.occurred_at = d.get("occurred_at", "")
    ctype = d.get("component_type", "")
    cinst = d.get("component_instance", "")
    row.component_ref = f"{ctype}.{cinst}" if ctype else d.get("component_ref", "")
    row.metrics = d.get("metrics", {}) or {}
    row.status_codes = list(d.get("status_codes", []))
    row.operating_condition = d.get("operating_condition", "")
    row.operator_id = d.get("operator_id", "")
    row.operator_version = d.get("operator_version", "")
    row.binding_revision = d.get("binding_revision", "")
    row.raw_log_ref = d.get("raw_log_ref", "")


def list_telemetry(session: Session, *, tenant_id: str,
                   device_sn: str | None = None) -> list[dict[str, Any]]:
    q = session.query(M.TelemetryEventRow).filter(
        M.TelemetryEventRow.tenant_id == tenant_id)
    if device_sn is not None:
        q = q.filter(M.TelemetryEventRow.device_sn == device_sn)
    q = q.order_by(M.TelemetryEventRow.created_at)
    return [_row_dict(r) for r in q.all()]


# ---------------------------------------------------------------- 用户知识文档

def save_knowledge_document(session: Session, *, tenant_id: str,
                            meta: dict[str, Any],
                            chunks: list[dict[str, Any]]) -> None:
    """写入一份用户导入文档及其切片（**幂等**：同 document_id 覆盖）。

    内容寻址：document_id 由内容哈希派生，同内容重复导入 → 同 id。先删该文档
    的旧切片再写新切片——避免改版内容残留旧块（与"覆盖"语义一致）。
    """
    doc_id = meta["document_id"]
    row = session.get(M.KnowledgeDocumentRow, (tenant_id, doc_id))
    if row is None:
        row = M.KnowledgeDocumentRow(tenant_id=tenant_id, document_id=doc_id)
        session.add(row)
    row.source_name = meta.get("source_name", "")
    row.content_sha256 = meta.get("content_sha256", "")
    row.char_count = int(meta.get("char_count", 0))
    row.chunk_count = len(chunks)
    # 覆盖语义：先清旧块
    (session.query(M.KnowledgeChunkRow)
     .filter(M.KnowledgeChunkRow.tenant_id == tenant_id,
             M.KnowledgeChunkRow.document_id == doc_id)
     .delete(synchronize_session=False))
    for c in chunks:
        session.add(M.KnowledgeChunkRow(
            tenant_id=tenant_id, chunk_id=c["chunk_id"], document_id=doc_id,
            ordinal=int(c.get("ordinal", 0)), text=c.get("text", "")))
    session.flush()


def list_knowledge_documents(session: Session, *, tenant_id: str
                             ) -> list[dict[str, Any]]:
    q = (session.query(M.KnowledgeDocumentRow)
         .filter(M.KnowledgeDocumentRow.tenant_id == tenant_id)
         .order_by(M.KnowledgeDocumentRow.created_at, M.KnowledgeDocumentRow.document_id))
    return [_row_dict(r) for r in q.all()]


def get_knowledge_document(session: Session, document_id: str, *,
                           tenant_id: str) -> dict[str, Any] | None:
    r = session.get(M.KnowledgeDocumentRow, (tenant_id, document_id))
    return _row_dict(r) if r else None


def list_knowledge_chunks(session: Session, *, tenant_id: str,
                          document_id: str | None = None) -> list[dict[str, Any]]:
    """取本租户的切片（可按文档过滤）。检索在内存里做——用户文档量级不大，
    且词法打分需要看全文，不做 DB 侧 LIKE（分词/大小写在各库上不一致）。"""
    q = session.query(M.KnowledgeChunkRow).filter(
        M.KnowledgeChunkRow.tenant_id == tenant_id)
    if document_id is not None:
        q = q.filter(M.KnowledgeChunkRow.document_id == document_id)
    q = q.order_by(M.KnowledgeChunkRow.document_id, M.KnowledgeChunkRow.ordinal)
    return [_row_dict(r) for r in q.all()]


def delete_knowledge_document(session: Session, document_id: str, *,
                              tenant_id: str) -> bool:
    """删一份文档及其切片（租户内）。返回是否存在并删除。"""
    row = session.get(M.KnowledgeDocumentRow, (tenant_id, document_id))
    if row is None:
        return False
    (session.query(M.KnowledgeChunkRow)
     .filter(M.KnowledgeChunkRow.tenant_id == tenant_id,
             M.KnowledgeChunkRow.document_id == document_id)
     .delete(synchronize_session=False))
    session.delete(row)
    session.flush()
    return True


# ---------------------------------------------------------------- 调查任务（G1）

# job 的终态：到达后不可再变（防止取消与完成互相覆盖）
_JOB_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def create_job(session: Session, *, tenant_id: str, job_id: str,
               candidate_id: str) -> dict[str, Any]:
    """写入一条 `pending` job。主键 (tenant_id, job_id)，重复提交同 id → 幂等更新。"""
    row = session.get(M.InvestigationJobRow, (tenant_id, job_id))
    if row is None:
        row = M.InvestigationJobRow(tenant_id=tenant_id, job_id=job_id)
        session.add(row)
    row.candidate_id = candidate_id
    row.status = "pending"
    row.report_id = None
    row.error_code = None
    row.worker_id = ""
    row.submitted_at = M._now()
    row.started_at = None
    row.finished_at = None
    session.flush()
    return _row_dict(row)


def get_job(session: Session, job_id: str, *,
            tenant_id: str) -> dict[str, Any] | None:
    """取单个 job。跨租户（或不存在）→ None（路由返回 404）。"""
    r = session.get(M.InvestigationJobRow, (tenant_id, job_id))
    return _row_dict(r) if r else None


def list_jobs(session: Session, *, tenant_id: str,
              status: str | None = None,
              limit: int | None = None,
              offset: int = 0) -> list[dict[str, Any]]:
    q = session.query(M.InvestigationJobRow).filter(
        M.InvestigationJobRow.tenant_id == tenant_id)
    if status is not None:
        q = q.filter(M.InvestigationJobRow.status == status)
    q = q.order_by(M.InvestigationJobRow.submitted_at, M.InvestigationJobRow.job_id)
    return _page(q, limit, offset)


def count_jobs_by_status(session: Session, *, tenant_id: str | None = None
                         ) -> dict[str, int]:
    """按 status 分组计数（不拉全表）。tenant_id=None → 全局（/metrics 用）。"""
    q = session.query(M.InvestigationJobRow.status, func.count())
    if tenant_id is not None:
        q = q.filter(M.InvestigationJobRow.tenant_id == tenant_id)
    q = q.group_by(M.InvestigationJobRow.status)
    return {s: int(n) for s, n in q.all()}


def claim_job(session: Session, job_id: str, *, tenant_id: str,
              worker_id: str) -> bool:
    """把 `pending` job 原子地置为 `running` 并记 worker_id。返回是否领取成功。

    用一条 `UPDATE ... WHERE status='pending'` 的**条件更新**做原子领取：数据库
    保证并发的两个 worker 只有一个的 WHERE 命中（rowcount=1），另一个 0 行 →
    返回 False。这比先 SELECT 再 UPDATE 安全（后者在竞态下两个都以为自己是首个）。
    """
    res = (session.query(M.InvestigationJobRow)
           .filter(M.InvestigationJobRow.tenant_id == tenant_id,
                   M.InvestigationJobRow.job_id == job_id,
                   M.InvestigationJobRow.status == "pending")
           .update({"status": "running", "worker_id": worker_id,
                    "started_at": M._now()}, synchronize_session=False))
    session.flush()
    return bool(res)


def finish_job(session: Session, job_id: str, *, tenant_id: str,
               status: str, report_id: str | None = None,
               error_code: str | None = None) -> bool:
    """把 job 置为终态（succeeded/failed）。已是终态 → 返回 False（不覆盖）。

    **只有 running 才能到 succeeded/failed**：防止"取消后 worker 才跑完也想写成功"
    把 cancelled 覆盖回 succeeded（取消是协作式的，见 investigation_jobs 注释）。
    """
    if status not in _JOB_TERMINAL:
        raise ValueError(f"非法终态: {status!r}")
    expected = "running"
    res = (session.query(M.InvestigationJobRow)
           .filter(M.InvestigationJobRow.tenant_id == tenant_id,
                   M.InvestigationJobRow.job_id == job_id,
                   M.InvestigationJobRow.status == expected)
           .update({"status": status, "report_id": report_id,
                    "error_code": error_code, "finished_at": M._now()},
                   synchronize_session=False))
    session.flush()
    return bool(res)


def list_stale_jobs(session: Session, *, status: str, older_than_s: float,
                    now: float | None = None,
                    limit: int | None = None) -> list[dict[str, Any]]:
    """列出"卡住"的 job（恢复扫描用）。**跨租户**（系统级后台扫描）。

    判据按状态取**不同时间戳**，因为它们回答的是不同问题：
      - `pending`：看 `submitted_at` —— "提交后多久还没被任何一个 worker 领走"
        （典型成因：worker 取件后、条件领取前崩溃）；
      - `running`：看 `started_at` —— "开工后多久还没结束"（典型成因：进程被杀）。
    """
    ts = float(M._now() if now is None else now)
    cutoff = ts - max(0.0, float(older_than_s))
    col = (M.InvestigationJobRow.started_at if status == "running"
           else M.InvestigationJobRow.submitted_at)
    q = (session.query(M.InvestigationJobRow)
         .filter(M.InvestigationJobRow.status == status,
                 col.isnot(None), col < cutoff)
         .order_by(col))
    if limit is not None:
        q = q.limit(max(0, int(limit)))
    return [_row_dict(r) for r in q.all()]


def cancel_job(session: Session, job_id: str, *, tenant_id: str) -> str | None:
    """请求取消 job。返回取消后的状态，或 None（跨租户/不存在）。

    - `pending` → 直接置 `cancelled`（worker 不会再领走它）。
    - `running` → 置 `cancelled`（协作式：worker 在步边界看到后停止）；
      注意此时 report 可能已写，故 worker 侧以 job 是否仍为 running 决定要不要提交产物。
    - 已终态 → 原样返回当前状态（不改）。
    """
    row = session.get(M.InvestigationJobRow, (tenant_id, job_id))
    if row is None:
        return None
    if row.status in _JOB_TERMINAL:
        return row.status
    row.status = "cancelled"
    row.finished_at = M._now()
    session.flush()
    return row.status


# ---------------------------------------------------------------- 变更记录（G7）

def save_change_record(session: Session, *, tenant_id: str,
                       record: dict[str, Any]) -> bool:
    """写入一条变更记录（幂等 upsert，主键 (tenant_id, change_id)）。返回是否新建。"""
    cid = record["change_id"]

    def apply(r: Any) -> None:
        r.kind = record.get("kind", "")
        r.target = record.get("target", "")
        r.applied_at = record.get("applied_at", "")
        r.from_version = record.get("from_version", "")
        r.to_version = record.get("to_version", "")
        r.actor = record.get("actor", "")
        r.source = record.get("source", "")
        r.raw = record.get("raw", {}) or {}

    return _upsert(session, M.ChangeRecordRow, (tenant_id, cid),
                   {"tenant_id": tenant_id, "change_id": cid}, apply)


def list_change_records(session: Session, *, tenant_id: str,
                        target: str | None = None,
                        limit: int | None = None,
                        offset: int = 0) -> list[dict[str, Any]]:
    """列出本租户的变更记录（可按 target 过滤）。"""
    q = session.query(M.ChangeRecordRow).filter(
        M.ChangeRecordRow.tenant_id == tenant_id)
    if target is not None:
        q = q.filter(M.ChangeRecordRow.target == target)
    q = q.order_by(M.ChangeRecordRow.applied_at, M.ChangeRecordRow.change_id)
    return _page(q, limit, offset)


# ---------------------------------------------------------------- 状态快照（G3）

def save_checkpoint(session: Session, *, checkpoint_id: str, prefix: str,
                    payload: dict[str, Any]) -> dict[str, Any]:
    """写入一份状态快照。同 id 幂等覆盖（正常每次 id 唯一）。"""
    row = session.get(M.StateCheckpointRow, checkpoint_id)
    if row is None:
        row = M.StateCheckpointRow(checkpoint_id=checkpoint_id)
        session.add(row)
    row.prefix = prefix
    row.n_keys = len(payload)
    row.payload = payload
    row.created_at = M._now()
    session.flush()
    return _row_dict(row)


def get_checkpoint(session: Session, checkpoint_id: str
                   ) -> dict[str, Any] | None:
    r = session.get(M.StateCheckpointRow, checkpoint_id)
    return _row_dict(r) if r else None


def latest_checkpoint(session: Session) -> dict[str, Any] | None:
    """取最近一份快照（按 created_at 倒序）。"""
    r = (session.query(M.StateCheckpointRow)
         .order_by(M.StateCheckpointRow.created_at.desc()).first())
    return _row_dict(r) if r else None


def list_checkpoints(session: Session, *, limit: int = 50
                     ) -> list[dict[str, Any]]:
    """列出快照元数据（**不含 payload**，避免一次拉出所有状态词典）。"""
    q = (session.query(M.StateCheckpointRow)
         .order_by(M.StateCheckpointRow.created_at.desc()).limit(limit))
    out = []
    for r in q.all():
        d = _row_dict(r)
        d.pop("payload", None)         # 列表只回元数据
        out.append(d)
    return out


def prune_checkpoints(session: Session, *, keep: int) -> int:
    """只保留最近 `keep` 份快照，删更旧的。返回删除数（保留策略，防表无限增长）。"""
    keep = max(0, keep)
    old = (session.query(M.StateCheckpointRow)
           .order_by(M.StateCheckpointRow.created_at.desc())
           .offset(keep).all())
    for r in old:
        session.delete(r)
    session.flush()
    return len(old)


# ---------------------------------------------------------------- 算子注册表（P4-2）

def upsert_operator_version(session: Session, *, tenant_id: str, operator_id: str,
                            version: str, state: str, dsl_source: str,
                            dsl_hash: str, signature: dict[str, Any],
                            rollout: dict[str, Any] | None = None) -> dict[str, Any]:
    """注册/更新一个算子版本（幂等按主键）。返回写入后的行。

    已存在则**仅更新内容与签名**，保留既有 state/rollout——注册一个已存在版本
    不应把它打回初始态（重注册只是刷新制品，不是重新发布）。
    """
    pk = (tenant_id, operator_id, version)
    row = session.get(M.OperatorRegistryRow, pk)
    created = row is None
    if row is None:
        row = M.OperatorRegistryRow(tenant_id=tenant_id, operator_id=operator_id,
                                    version=version)
        session.add(row)
    row.dsl_source = dsl_source
    row.dsl_hash = dsl_hash
    row.signature = signature
    if created:
        row.state = state
        row.rollout = rollout or {}
        row.metrics_snapshot = {}
    elif rollout is not None:
        row.rollout = rollout
    row.updated_at = M._now()
    session.flush()
    return _row_dict(row)


def get_operator_version(session: Session, *, tenant_id: str, operator_id: str,
                         version: str) -> dict[str, Any] | None:
    """取单个算子版本。跨租户（或不存在）→ None（路由返回 404）。"""
    r = session.get(M.OperatorRegistryRow, (tenant_id, operator_id, version))
    return _row_dict(r) if r else None


def list_operator_versions(session: Session, *, tenant_id: str,
                           operator_id: str | None = None
                           ) -> list[dict[str, Any]]:
    """列出本租户的算子版本（可按 operator_id 过滤），按创建时间排序。"""
    q = session.query(M.OperatorRegistryRow).filter(
        M.OperatorRegistryRow.tenant_id == tenant_id)
    if operator_id is not None:
        q = q.filter(M.OperatorRegistryRow.operator_id == operator_id)
    q = q.order_by(M.OperatorRegistryRow.created_at,
                   M.OperatorRegistryRow.operator_id,
                   M.OperatorRegistryRow.version)
    return [_row_dict(r) for r in q.all()]


def list_operators(session: Session, *, tenant_id: str) -> list[dict[str, Any]]:
    """按 operator_id 聚合：每个算子返回其**当前 Active 版本**（无则最新）。
    列表端点用。Active 优先，其次按版本创建时间取最新。"""
    rows = list_operator_versions(session, tenant_id=tenant_id)
    by_op: dict[str, dict[str, Any]] = {}
    for r in rows:
        cur = by_op.get(r["operator_id"])
        if cur is None:
            by_op[r["operator_id"]] = r
            continue
        # Active 版本优先；否则取创建更晚的
        r_active = r["state"] == "Active"
        cur_active = cur["state"] == "Active"
        if (r_active and not cur_active) or (
                r_active == cur_active and r["created_at"] > cur["created_at"]):
            by_op[r["operator_id"]] = r
    return sorted(by_op.values(), key=lambda x: x["operator_id"])


def set_operator_state(session: Session, *, tenant_id: str, operator_id: str,
                       version: str, state: str,
                       rollout: dict[str, Any] | None = None,
                       metrics_snapshot: dict[str, Any] | None = None
                       ) -> dict[str, Any] | None:
    """更新一个算子版本的状态（及可选 rollout/指标快照）。**不做合法性校验**——
    由调用方（registry 领域层，持有状态机）判断后再调用。跨租户/不存在 → None。"""
    row = session.get(M.OperatorRegistryRow, (tenant_id, operator_id, version))
    if row is None:
        return None
    row.state = state
    if rollout is not None:
        row.rollout = rollout
    if metrics_snapshot is not None:
        row.metrics_snapshot = metrics_snapshot
    row.updated_at = M._now()
    session.flush()
    return _row_dict(row)


def record_rollout_event(session: Session, *, tenant_id: str, operator_id: str,
                         version: str, from_state: str | None, to_state: str,
                         reason: str, actor: str) -> None:
    """追加一条状态转移台账（审计）。"""
    session.add(M.OperatorRolloutEventRow(
        tenant_id=tenant_id, operator_id=operator_id, version=version,
        from_state=from_state, to_state=to_state, reason=reason, actor=actor))
    session.flush()


def list_rollout_events(session: Session, *, tenant_id: str,
                        operator_id: str, version: str,
                        limit: int | None = None) -> list[dict[str, Any]]:
    """取某算子版本的转移台账（租户内），按时间正序。"""
    q = (session.query(M.OperatorRolloutEventRow)
         .filter(M.OperatorRolloutEventRow.tenant_id == tenant_id,
                 M.OperatorRolloutEventRow.operator_id == operator_id,
                 M.OperatorRolloutEventRow.version == version)
         .order_by(M.OperatorRolloutEventRow.at, M.OperatorRolloutEventRow.id))
    rows = q.all()
    if limit is not None:
        rows = rows[-limit:]
    return [_row_dict(r) for r in rows]


def find_previous_signed_version(session: Session, *, tenant_id: str,
                                 operator_id: str,
                                 before_created_at: float) -> dict[str, Any] | None:
    """找同一算子**早于**给定时间、且签名非空的最近一个版本——回滚目标。

    回滚是"回到上一签名版本"，不是删除当前版本（对齐 lifecycle 语义）。
    """
    q = (session.query(M.OperatorRegistryRow)
         .filter(M.OperatorRegistryRow.tenant_id == tenant_id,
                 M.OperatorRegistryRow.operator_id == operator_id,
                 M.OperatorRegistryRow.created_at < before_created_at)
         .order_by(M.OperatorRegistryRow.created_at.desc()))
    for r in q.all():
        if r.signature:  # 有签名才算"可回滚到的版本"
            return _row_dict(r)
    return None


# ---------------------------------------------------------------- 值班表（P4-3）

def upsert_rota(session: Session, *, tenant_id: str, rota_id: str, domain: str,
                level: int, assignee: str, start_at: float, end_at: float,
                channel: str = "webhook", target: str = "") -> dict[str, Any]:
    """写入/更新一条值班条目（按主键幂等）。同一排班反复 PUT 收敛到同一行。"""
    pk = (tenant_id, rota_id)
    row = session.get(M.OncallRotaRow, pk)
    if row is None:
        row = M.OncallRotaRow(tenant_id=tenant_id, rota_id=rota_id)
        session.add(row)
    row.domain = domain
    row.level = int(level)
    row.assignee = assignee
    row.start_at = float(start_at)
    row.end_at = float(end_at)
    row.channel = channel or "webhook"
    row.target = target or ""
    session.flush()
    return _row_dict(row)


def list_rota(session: Session, *, tenant_id: str,
              domain: str | None = None) -> list[dict[str, Any]]:
    """列出本租户的值班条目（可按领域过滤），按起始时间排序。"""
    q = session.query(M.OncallRotaRow).filter(
        M.OncallRotaRow.tenant_id == tenant_id)
    if domain is not None:
        q = q.filter(M.OncallRotaRow.domain == domain)
    q = q.order_by(M.OncallRotaRow.start_at, M.OncallRotaRow.level)
    return [_row_dict(r) for r in q.all()]


def delete_rota(session: Session, *, tenant_id: str, rota_id: str) -> bool:
    """删除一条值班条目。返回是否删除（不存在 → False）。"""
    row = session.get(M.OncallRotaRow, (tenant_id, rota_id))
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


# ---------------------------------------------------------------- 告警投递台账（P4-3）

def upsert_delivery(session: Session, *, tenant_id: str, delivery_id: str,
                    event_id: str, level: int, channel: str, target: str,
                    assignee: str, status: str, sent_at: float | None,
                    ack_deadline: float | None, last_error: str = "",
                    bump_attempts: bool = True, domain: str = "*") -> dict[str, Any]:
    """写入/更新一条投递台账（按 (tenant_id, delivery_id) 幂等）。

    `delivery_id = hash(event_id, level)` → 同一告警同一级只投一次；重复触发是**更新**
    （attempts+1、刷新 sent_at/deadline）而非新增行——这正是"幂等"的落点。
    """
    pk = (tenant_id, delivery_id)
    row = session.get(M.AlertDeliveryRow, pk)
    if row is None:
        row = M.AlertDeliveryRow(tenant_id=tenant_id, delivery_id=delivery_id)
        session.add(row)
        attempts = 1 if bump_attempts else 0
    else:
        attempts = int(row.attempts or 0) + (1 if bump_attempts else 0)
    row.event_id = event_id
    row.level = int(level)
    row.domain = domain or "*"
    row.channel = channel
    row.target = target
    row.assignee = assignee
    row.status = status
    row.attempts = attempts
    row.sent_at = sent_at
    row.ack_deadline = ack_deadline
    if last_error:
        row.last_error = last_error
    session.flush()
    return _row_dict(row)


def get_delivery(session: Session, *, tenant_id: str,
                 delivery_id: str) -> dict[str, Any] | None:
    r = session.get(M.AlertDeliveryRow, (tenant_id, delivery_id))
    return _row_dict(r) if r else None


def list_deliveries(session: Session, *, tenant_id: str,
                    event_id: str | None = None, status: str | None = None,
                    limit: int | None = None) -> list[dict[str, Any]]:
    """列出本租户的投递记录（可按告警/状态过滤），按创建时间排序。"""
    q = session.query(M.AlertDeliveryRow).filter(
        M.AlertDeliveryRow.tenant_id == tenant_id)
    if event_id is not None:
        q = q.filter(M.AlertDeliveryRow.event_id == event_id)
    if status is not None:
        q = q.filter(M.AlertDeliveryRow.status == status)
    q = q.order_by(M.AlertDeliveryRow.created_at, M.AlertDeliveryRow.delivery_id)
    return _page(q, limit, 0)


def ack_deliveries(session: Session, *, tenant_id: str, event_id: str,
                   at: float) -> int:
    """把某告警**所有 pending** 投递标记为 acked（停止升级）。返回改动条数。

    ack 是"人看到了"，故同一告警当前还在等待的所有级别一起停——一处确认即止损，
    不要求逐级 ack。
    """
    q = (session.query(M.AlertDeliveryRow)
         .filter(M.AlertDeliveryRow.tenant_id == tenant_id,
                 M.AlertDeliveryRow.event_id == event_id,
                 M.AlertDeliveryRow.status == "pending"))
    n = 0
    for row in q.all():
        row.status = "acked"
        row.acked_at = at
        n += 1
    session.flush()
    return n


def set_delivery_status(session: Session, *, tenant_id: str, delivery_id: str,
                        status: str, last_error: str = "") -> None:
    """更新单条投递状态（升级/失败时用）。"""
    row = session.get(M.AlertDeliveryRow, (tenant_id, delivery_id))
    if row is None:
        return
    row.status = status
    if last_error:
        row.last_error = last_error
    session.flush()


def list_due_deliveries(session: Session, *, now: float,
                        limit: int | None = None) -> list[dict[str, Any]]:
    """扫**所有租户**中「pending 且已过 ack_deadline」的投递（升级 sweeper 用）。

    **故意不按租户过滤**：升级引擎是系统级后台进程，须跨租户找出所有超时未确认的
    告警——与请求路径的租户作用域规则不同，此处是受控的系统扫描。调用方（sweeper）
    对每条命中的行按其自带 tenant_id 单独会话处理（RLS 下逐租户放行）。
    """
    q = (session.query(M.AlertDeliveryRow)
         .filter(M.AlertDeliveryRow.status == "pending",
                 M.AlertDeliveryRow.ack_deadline.isnot(None),
                 M.AlertDeliveryRow.ack_deadline <= now)
         .order_by(M.AlertDeliveryRow.ack_deadline))
    return _page(q, limit, 0)
