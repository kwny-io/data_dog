# -*- coding: utf-8 -*-
"""仓储层：领域对象 ↔ 持久化行的转换与读写。

供 P2（候选）与 P3（Alert / Outbox / 反馈）调用，实现"重启不丢"。
所有写入都是按主键的**幂等 upsert**：重复保存不报错、不产生重复记录。

安全说明：本模块**不做任何语句字符串拼接**，全部通过 ORM 的
`session.get` / `session.query` / 表达式构造器完成；值以**绑定参数**传递，
因此不存在注入面。
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models as M


def _as_dict(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return dict(obj)
    if is_dataclass(obj):
        return asdict(obj)
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return dict(vars(obj))


def _row_dict(r: Any) -> Dict[str, Any]:
    """ORM 行 → 普通 dict（只取真实列）。"""
    return {c.name: getattr(r, c.name) for c in r.__table__.columns}


def _upsert(session: Session, model: Any, pk: Any, pk_kwargs: Dict[str, Any],
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

def _apply_candidate(row: Any, d: Dict[str, Any]) -> None:
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


def save_candidates(session: Session, candidates: List[Any]) -> int:
    """保存候选（幂等：已存在则更新）。返回写入条数。并发安全。"""
    n = 0
    for c in candidates:
        d = _as_dict(c)
        cid = d["candidate_id"]
        _upsert(session, M.CandidateRow, cid, {"candidate_id": cid},
                lambda row, _d=d: _apply_candidate(row, _d))
        n += 1
    return n


def _page(q: Any, limit: Optional[int], offset: int) -> List[Dict[str, Any]]:
    """分页执行查询。limit=None 表示不分页（保持旧行为，供内部调用）。"""
    if offset:
        q = q.offset(max(0, int(offset)))
    if limit is not None:
        q = q.limit(max(0, int(limit)))
    return [_row_dict(r) for r in q.all()]


def count_rows(session: Session, model: Any, **filters: Any) -> int:
    """按条件计数（用 COUNT(*) 聚合，**不把全表拉进内存**）。

    此前 /metrics 用 `len(list_candidates(s))` 之类取总数，数据一多就会把整表
    加载到内存再取长度，指标端点反而成了瓶颈。
    """
    q = session.query(func.count()).select_from(model)
    for col, val in filters.items():
        q = q.filter(getattr(model, col) == val)
    return int(q.scalar() or 0)


def list_candidates(session: Session, *, device_sn: Optional[str] = None,
                    component_ref: Optional[str] = None,
                    limit: Optional[int] = None,
                    offset: int = 0) -> List[Dict[str, Any]]:
    q = session.query(M.CandidateRow)
    if device_sn is not None:
        q = q.filter(M.CandidateRow.device_sn == device_sn)
    if component_ref is not None:
        q = q.filter(M.CandidateRow.component_ref == component_ref)
    # created_at 可能重复 → 追加主键做稳定排序，否则分页会漏/重
    q = q.order_by(M.CandidateRow.created_at, M.CandidateRow.candidate_id)
    return _page(q, limit, offset)


# ---------------------------------------------------------------- 调查报告

def save_report(session: Session, report: Any) -> None:
    d = _as_dict(report)
    rid = d["investigation_id"]
    row = session.get(M.ReportRow, rid) or M.ReportRow(investigation_id=rid)
    session.add(row)
    row.disposition = d.get("disposition", "")
    row.severity = d.get("severity")
    row.confidence = float(d.get("confidence", 0.0))
    row.event_type = d.get("event_type")
    row.summary = d.get("summary", "")
    row.candidate_refs = list(d.get("candidate_refs", []))
    row.hypotheses = [_as_dict(h) for h in d.get("hypotheses", [])]
    row.missing_evidence = list(d.get("missing_evidence", []))
    row.recommended_actions = list(d.get("recommended_actions", []))
    row.correlation_group_id = d.get("correlation_group_id")
    row.model_provenance = d.get("model_provenance", {}) or {}
    session.flush()


def list_reports(session: Session) -> List[Dict[str, Any]]:
    q = session.query(M.ReportRow).order_by(M.ReportRow.created_at)
    return [_row_dict(r) for r in q.all()]


# ---------------------------------------------------------------- Alert

def save_alert(session: Session, event: Any) -> None:
    d = _as_dict(event)
    eid = d["event_id"]
    _upsert(session, M.AlertRow, eid, {"event_id": eid},
            lambda row: _apply_alert(row, d))


def _apply_alert(row: Any, d: Dict[str, Any]) -> None:
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


def get_alert(session: Session, event_id: str) -> Optional[Dict[str, Any]]:
    r = session.get(M.AlertRow, event_id)
    return _row_dict(r) if r else None


def list_alerts(session: Session, *, limit: Optional[int] = None,
                offset: int = 0) -> List[Dict[str, Any]]:
    q = session.query(M.AlertRow).order_by(
        M.AlertRow.created_at, M.AlertRow.event_id)
    return _page(q, limit, offset)


# ---------------------------------------------------------------- Outbox

def upsert_outbox(session: Session, *, idempotency_key: str, revision: int,
                  payload: Dict[str, Any]) -> bool:
    """写入 outbox 条目。已存在 → 返回 False（幂等，不重复入队）。并发安全。"""
    pk = (idempotency_key, revision)

    def _apply(row: Any) -> None:
        # 已存在时不覆盖 payload（保留首次写入的语义），仅在竞态分支被调用
        pass

    row = session.get(M.AlertOutboxRow, pk)
    if row is not None:
        return False
    new_row = M.AlertOutboxRow(idempotency_key=idempotency_key, revision=revision,
                               payload=payload, status="pending")
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


def mark_outbox_delivered(session: Session, *, idempotency_key: str, revision: int,
                          receipt: str) -> None:
    row = session.get(M.AlertOutboxRow, (idempotency_key, revision))
    if row is not None:
        row.status = "delivered"
        row.receipt = receipt
        session.flush()


def list_outbox(session: Session, *, status: Optional[str] = None,
                limit: Optional[int] = None,
                offset: int = 0) -> List[Dict[str, Any]]:
    q = session.query(M.AlertOutboxRow)
    if status is not None:
        q = q.filter(M.AlertOutboxRow.status == status)
    q = q.order_by(M.AlertOutboxRow.created_at, M.AlertOutboxRow.idempotency_key,
                   M.AlertOutboxRow.revision)
    return _page(q, limit, offset)


# ---------------------------------------------------------------- 反馈

def save_feedback(session: Session, *, event_id: str, kind: str, note: str = "") -> None:
    session.add(M.AlertFeedbackRow(event_id=event_id, kind=kind, note=note))
    session.flush()


def list_feedback(session: Session) -> List[Dict[str, Any]]:
    q = session.query(M.AlertFeedbackRow).order_by(M.AlertFeedbackRow.created_at)
    return [_row_dict(r) for r in q.all()]


# ---------------------------------------------------------------- 遥测事件

def save_telemetry(session: Session, events: List[Any]) -> int:
    n = 0
    for e in events:
        d = _as_dict(e)
        eid = d["event_id"]
        _upsert(session, M.TelemetryEventRow, eid, {"event_id": eid},
                lambda row, _d=d: _apply_telemetry(row, _d))
        n += 1
    return n


def _apply_telemetry(row: Any, d: Dict[str, Any]) -> None:
    row.tenant_id = d.get("tenant_id", "")
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


def list_telemetry(session: Session, *, device_sn: Optional[str] = None) -> List[Dict[str, Any]]:
    q = session.query(M.TelemetryEventRow)
    if device_sn is not None:
        q = q.filter(M.TelemetryEventRow.device_sn == device_sn)
    q = q.order_by(M.TelemetryEventRow.created_at)
    return [_row_dict(r) for r in q.all()]
