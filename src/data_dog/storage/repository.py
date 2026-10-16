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


def delete_in_batches(session: Session, table: Any, cond: Any, *,
                      batch_size: int = 500, max_batches: int = 200
                      ) -> tuple[int, int]:
    """按条件**有界分批**删除，返回 `(deleted, batches)`。

    为什么不是一条 `DELETE WHERE ...`：大表上长事务会持锁、把热路径写入堵住
    （本项目已经因为"同步落库排队"在 P99 上吃过亏，见 `docs/write_path_batching.md`）。
    为什么不是 `DELETE ... LIMIT`：`LIMIT` **不是标准 SQL**（PG 不支持；SQLite 要编译期
    开关）。改成**按主键先查后删**，两边都成立。

    主键从 `table.primary_key` 取（**不硬编码**），所以换了复合主键也不用改这里。
    批数用满即返回——调用方据 `batches == max_batches` 判断"本轮没清完"，下轮继续，
    而不是"为了清完把库锁死"。

    两个调用方共用同一处：④ 的保留清理（按时间列）与 ⑥ 的租户级删除（按 `tenant_id`）。
    `cond` 由调用方用**表达式构造器**拼出（本模块不拼 SQL 字符串），值走绑定参数。
    """
    from sqlalchemy import delete, select, tuple_

    pk = list(table.primary_key.columns)
    if not pk:                                    # pragma: no cover - 本仓表都有主键
        raise RuntimeError(f"{table.name} 没有主键，无法分批删除")

    deleted = 0
    batches = 0
    while batches < max_batches:
        keys = [tuple(k) for k in session.execute(
            select(*pk).where(cond).limit(batch_size)).all()]
        if not keys:
            break
        if len(pk) == 1:
            stmt = delete(table).where(pk[0].in_([k[0] for k in keys]))
        else:
            stmt = delete(table).where(tuple_(*pk).in_(keys))
        res = session.execute(stmt)
        deleted += int(getattr(res, "rowcount", 0) or 0)   # CursorResult 有 rowcount，Result 的存根没有
        batches += 1
    return deleted, batches


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


# upsert 语句缓存：键 = (方言, 表名)。语句结构只随表变化，**与数据无关**。
_UPSERT_STMT_CACHE: dict[tuple[str, str], Any] = {}


def _upsert_stmt(model: Any, dialect: str) -> Any:
    """按 `(方言, 表)` **缓存** upsert 语句；方言不支持返回 None。

    为什么必须缓存（实测出来的纯浪费）
    ---------------------------------
    `docs/write_path_batching.md` §9 的剖析：每请求只**执行 2 条语句**，却有
    **28 次** SQLAlchemy schema 对象构造（`schema.__init__` / `_make_proxy` /
    `visit_bindparam` / `_append_param_parameter`）。原因是旧写法每行都
    `insert(model).values(...).on_conflict_do_update(...)` —— **重建整个 Core 语句对象**。
    而语句结构只随表变化、跟数据无关，每行重建纯属浪费 CPU（HTTP+落库层实测
    **7.76 ms/请求**，是数据面 0.039 ms 的 **200 倍**，单核上限因此只有 ~129 req/s）。

    值改由 `session.execute(stmt, params)` 传入：**省掉的是 Python 侧的语句对象构造**。

    ⚠️ 更正（2026-10-12）：此前这里和服务文档 §9.3 都声称"能让编译缓存命中同一条"，
    **那是错的**。SQLAlchemy 的方言 `Insert`（`dialects/{postgresql,sqlite}/dml.py`）
    显式设了 `inherit_cache = False`，其 `OnConflictDoUpdate` 子句的 `inherit_cache`
    也未设置 —— 于是 `_generate_cache_key()` 返回 `None`，**每次执行都重新编译**。
    实测：50 次执行 → `visit_on_conflict_do_update` **50 次**、`bindparam_string`
    **750 次**，`engine._compiled_cache` 始终为空。没有干净的修法（实例级
    `inherit_cache` 无效；子类化控制不到内部构造的 `OnConflictDoUpdate`；改 `text()`
    会丢掉 `created_at` 等列默认值、NOT NULL 直接失败；预编译对象不能经 `Session.execute`
    执行）——故**保持现状**，只把结论改正。

    更新列**从表结构推导**（非主键、非 `created_at`），而不是从本次的 values 推导——
    这样"哪些列该被 upsert 覆盖"只写一遍，不会与 `_*_values()` 的字段清单漂移。
    """
    key = (dialect, model.__tablename__)
    cached = _UPSERT_STMT_CACHE.get(key)
    if cached is not None:
        return cached
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    elif dialect == "sqlite":
        # 两种方言的 insert 变体是同名不同类型：mypy 会判"不兼容导入"，运行时无碍。
        from sqlalchemy.dialects.sqlite import insert as _insert  # type: ignore[assignment]
    else:
        return None

    base = _insert(model)
    pk = [c.name for c in model.__table__.primary_key.columns]
    keep_out = set(pk) | {"created_at"}     # created_at = 首次写入时间，不覆盖（见下）
    upd = [c.name for c in model.__table__.columns if c.name not in keep_out]
    stmt = base.on_conflict_do_update(
        index_elements=pk, set_={c: base.excluded[c] for c in upd})
    _UPSERT_STMT_CACHE[key] = stmt
    return stmt


def _upsert_one_stmt(session: Session, model: Any, pk_kwargs: dict[str, Any],
                     values: dict[str, Any]) -> bool:
    """**一条语句**的幂等 upsert；方言不支持时返回 False（调用方回退到 `_upsert`）。

    为什么要换掉"get + SAVEPOINT + INSERT + RELEASE"（每行 4 条语句 → **1 条**）
    --------------------------------------------------------------------------
    旧写法的存在理由是扛"并发两个请求同时 `get` 到 None"这个竞态（实测 8 并发写同一 id
    会有 2 个抛 `IntegrityError`）：把 INSERT 包进 SAVEPOINT，撞唯一键就回滚 SAVEPOINT
    再改更新。**它是正确的**——但代价是每行 4 条语句，而且**两次 `SAVEPOINT`/`RELEASE`
    与 upsert 前的两个 `get` 合起来占掉每请求约 5 条**
    （`docs/write_path_batching.md` §7.1 的实测清单）。

    `INSERT … ON CONFLICT DO UPDATE` 把同一件事故交给数据库**原子**完成：
    **既是 1 条语句，又根本没有"先查后插"的窗口**——竞态从"被兜住"变成"不存在"。
    这与 ④ 的分批删除是同一类判断：**能交给数据库一次做完的事，不要拆成多次往返。**

    `created_at` **不进更新集**：它是**首次写入**的时间，也是 ④ 保留策略的判据
    （按 ingestion 时间清理）。重复投递若把它刷新成"刚写入"，一条被反复重投的老事件
    就**永远不会被清理**——那会让保留策略静默失效。
    """
    try:
        dialect = session.get_bind().dialect.name
    except Exception:                             # noqa: BLE001 - 拿不到方言就走回退
        return False
    stmt = _upsert_stmt(model, dialect)
    if stmt is None:
        return False
    session.execute(stmt, {**pk_kwargs, **values})
    return True


# ---------------------------------------------------------------- 候选

def _candidate_values(d: dict[str, Any]) -> dict[str, Any]:
    """候选行的**列值**（唯一来源：ORM 写入与 Core upsert 共用它，避免两处漂移）。"""
    return {
        "device_sn": d.get("device_sn", ""),
        "component_ref": d.get("component_ref", ""),
        "signal": d.get("signal", ""),
        "severity_hint": d.get("severity_hint", "info"),
        "window_start": d.get("window_start", ""),
        "window_end": d.get("window_end", ""),
        "sample_count": int(d.get("sample_count", 0)),
        "rule_matches": list(d.get("rule_matches", [])),
        "anomaly_score": float(d.get("anomaly_score", 0.0)),
        "evidence": [e if isinstance(e, dict) else _as_dict(e)
                     for e in d.get("evidence", [])],
        "evidence_refs": list(d.get("evidence_refs", [])),
        "detector_provenance": d.get("detector_provenance", ""),
    }


def _apply_candidate(row: Any, d: dict[str, Any]) -> None:
    for k, v in _candidate_values(d).items():
        setattr(row, k, v)


def save_candidates(session: Session, candidates: list[Any], *, tenant_id: str) -> int:
    """保存候选（幂等：已存在则更新）。返回写入条数。并发安全。

    主键是 (tenant_id, candidate_id)：candidate_id 由设备/部件/信号派生、
    不含租户，故必须用复合主键，否则跨租户同名 id 会互相覆盖。
    """
    n = 0
    for c in candidates:
        d = _as_dict(c)
        cid = d["candidate_id"]
        pk = {"tenant_id": tenant_id, "candidate_id": cid}

        if not _upsert_one_stmt(session, M.CandidateRow, pk, _candidate_values(d)):
            # 方言不支持 ON CONFLICT 时回退到旧路径（正确性优先于语句数）
            def apply_fn(row: Any, _d: dict[str, Any] = d) -> None:  # noqa: B023
                _apply_candidate(row, _d)

            _upsert(session, M.CandidateRow, (tenant_id, cid), pk, apply_fn)
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

def save_telemetry(session: Session, events: list[Any], *, tenant_id: str,
                   now: float | None = None) -> int:
    """保存规范化遥测。**租户来自调用方（认证上下文），忽略事件正文里的 tenant_id**
    ——与「身份只在网关注入、正文自称一律丢弃」的规则一致。

    主键是 `(tenant_id, event_id, created_day)`，`created_day = floor(created_at / 86400)`
    （A9 分区的前置条件：PG 要求分区表的唯一约束包含分区键）：

    - `created_at` 与 `created_day` 由**同一时刻**算出（`now` 只是测试注入点），所以不会出现
      "跨午夜时两者差一天"；
    - **同日**重投递仍命中同一主键 → 幂等（绝大多数重试都是同日）；**跨天重投递会多一行**
      ——已知代价，检测见 docs/storage.md §8.2；
    - `created_at` 仍是**首次写入**时间（不进更新集），保留策略继续按**摄取时间**清理。
    """
    n = 0
    for e in events:
        d = _as_dict(e)
        eid = d["event_id"]
        ts = M._now() if now is None else float(now)
        day = M.day_of(ts)
        pk = {"tenant_id": tenant_id, "event_id": eid, "created_day": day}

        if not _upsert_one_stmt(session, M.TelemetryEventRow, pk,
                                {**_telemetry_values(d), "created_at": ts}):
            def apply_fn(row: Any, _d: dict[str, Any] = d,          # noqa: B023
                         _ts: float = ts, _day: int = day) -> None:
                _apply_telemetry(row, _d)
                if row.created_at is None:      # 新行：与 created_day 同一时刻
                    row.created_at = _ts
                    row.created_day = _day

            _upsert(session, M.TelemetryEventRow, (tenant_id, eid, day), pk, apply_fn)
        n += 1
    return n


def _telemetry_values(d: dict[str, Any]) -> dict[str, Any]:
    """遥测行的**列值**（唯一来源：ORM 写入与 Core upsert 共用它）。

    `tenant_id` 不在这里：它来自认证上下文（`save_telemetry` 的显式入参），
    **绝不从事件正文取**——正文的身份声明是不可信的。
    """
    ctype = d.get("component_type", "")
    cinst = d.get("component_instance", "")
    return {
        "device_sn": d.get("device_sn", ""),
        "occurred_at": d.get("occurred_at", ""),
        "component_ref": f"{ctype}.{cinst}" if ctype else d.get("component_ref", ""),
        "metrics": d.get("metrics", {}) or {},
        "status_codes": list(d.get("status_codes", [])),
        "operating_condition": d.get("operating_condition", ""),
        "operator_id": d.get("operator_id", ""),
        "operator_version": d.get("operator_version", ""),
        "binding_revision": d.get("binding_revision", ""),
        "raw_log_ref": d.get("raw_log_ref", ""),
    }


def _apply_telemetry(row: Any, d: dict[str, Any]) -> None:
    for k, v in _telemetry_values(d).items():
        setattr(row, k, v)


def count_cross_day_duplicates(session: Session, *, tenant_id: str | None = None,
                               since: float | None = None) -> list[tuple[str, str, int]]:
    """跨天重投递的**检测**：同一 `(tenant_id, event_id)` 出现在多个日桶里（返回 n>1 的项）。

    为什么需要它：`created_day` 进主键换来"能按时间分区"，代价是**跨天重投递不再去重**
    （同日重投递仍然幂等，见 `save_telemetry`）。没有检测，"代价"就只是一句空话——
    这是把已知代价变成**可观测事实**的最小实现。

    成本：`GROUP BY` 扫描（分区表上 `since` 能触发分区裁剪）。**刻意不挂在 `/metrics` 或
    retention 的每次运行上**（那是热路径/后台热路径），由运维按需/定期跑。
    """
    q = session.query(M.TelemetryEventRow.tenant_id, M.TelemetryEventRow.event_id,
                      func.count().label("n"))
    if tenant_id is not None:
        q = q.filter(M.TelemetryEventRow.tenant_id == tenant_id)
    if since is not None:
        q = q.filter(M.TelemetryEventRow.created_at >= since)
    q = (q.group_by(M.TelemetryEventRow.tenant_id, M.TelemetryEventRow.event_id)
          .having(func.count() > 1))
    return [(str(t), str(e), int(n)) for t, e, n in q.all()]


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


# ---------------------------------------------------------------- 调查运行日志

def save_run_log(session: Session, log: dict[str, Any], *, tenant_id: str) -> None:
    """落一次调查的运行日志（按 (tenant_id, run_id) 幂等 upsert）。

    两条路径共用：同步端点自己写；异步 worker 在 **job 终态同事务**里写（见
    service/investigation_jobs._finish）——"job 有终态 ⇔ 有日志"才成立。
    """
    rid = str(log["run_id"])
    _upsert(session, M.InvestigationRunLogRow, (tenant_id, rid),
            {"tenant_id": tenant_id, "run_id": rid},
            lambda row: _apply_run_log(row, log))


def _apply_run_log(row: Any, d: dict[str, Any]) -> None:
    row.candidate_id = str(d.get("candidate_id", ""))
    row.kind = str(d.get("kind", "sync"))
    row.status = str(d.get("status", ""))
    row.report_id = d.get("report_id")
    row.error_code = d.get("error_code")
    row.error_detail = str(d.get("error_detail", ""))
    row.model = str(d.get("model", ""))
    row.step_count = int(d.get("step_count", 0))
    row.duration_ms = float(d.get("duration_ms", 0.0))
    row.steps = list(d.get("steps", []) or [])
    row.usage = dict(d.get("usage", {}) or {})


def get_run_log(session: Session, run_id: str, *,
                tenant_id: str) -> dict[str, Any] | None:
    """取一条运行日志。跨租户（或不存在）→ None（路由返回 404，不泄露存在性）。"""
    r = session.get(M.InvestigationRunLogRow, (tenant_id, run_id))
    return _row_dict(r) if r else None


def list_run_logs(session: Session, *, tenant_id: str,
                  candidate_id: str | None = None,
                  status: str | None = None,
                  limit: int | None = None,
                  offset: int = 0) -> list[dict[str, Any]]:
    """按候选/状态翻运行日志，**新的在前**（排查时先看最近那次）。"""
    q = session.query(M.InvestigationRunLogRow).filter(
        M.InvestigationRunLogRow.tenant_id == tenant_id)
    if candidate_id is not None:
        q = q.filter(M.InvestigationRunLogRow.candidate_id == candidate_id)
    if status is not None:
        q = q.filter(M.InvestigationRunLogRow.status == status)
    q = q.order_by(M.InvestigationRunLogRow.created_at.desc(),
                   M.InvestigationRunLogRow.run_id)
    return _page(q, limit, offset)


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


# ---------------------------------------------------------------- 安全审计（A1）

def save_audit(session: Session, *, tenant_id: str, audit_id: str, ts: float,
               actor: str = "", action: str = "", target: str = "",
               outcome: str = "", reason: str = "", request_id: str = "",
               trace_id: str = "", key_id: str = "", source_ip: str = "",
               detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """写一条审计记录。**只增不改**——审计被改写就失去证据价值（故没有 update 路径）。

    `audit_id` 唯一但**不幂等**：两次相同事件就是两条事实，与业务台账（按确定性 id
    UPSERT）相反。
    """
    row = M.AuditLogRow(tenant_id=tenant_id, audit_id=audit_id)
    row.ts = float(ts)
    row.actor = actor
    row.action = action
    row.target = target
    row.outcome = outcome
    row.reason = reason
    row.request_id = request_id
    row.trace_id = trace_id
    row.key_id = key_id
    row.source_ip = source_ip
    row.detail = dict(detail or {})
    session.add(row)
    session.flush()
    return _row_dict(row)


def _audit_query(session: Session, *, tenant_id: str | None, action: str | None,
                 outcome: str | None, actor: str | None, since: float | None,
                 until: float | None):
    q = session.query(M.AuditLogRow)
    if tenant_id is not None:
        q = q.filter(M.AuditLogRow.tenant_id == tenant_id)
    if action:
        q = q.filter(M.AuditLogRow.action == action)
    if outcome:
        q = q.filter(M.AuditLogRow.outcome == outcome)
    if actor:
        q = q.filter(M.AuditLogRow.actor == actor)
    if since is not None:
        q = q.filter(M.AuditLogRow.ts >= float(since))
    if until is not None:
        q = q.filter(M.AuditLogRow.ts <= float(until))
    return q


def list_audit(session: Session, *, tenant_id: str | None,
               action: str | None = None, outcome: str | None = None,
               actor: str | None = None, since: float | None = None,
               until: float | None = None, limit: int | None = None,
               offset: int = 0) -> list[dict[str, Any]]:
    """按条件翻审计记录（**新的在前**）。

    `tenant_id=None` = **跨租户**：只给系统/运维查询用（调用方必须走
    `session_scope(system=True)`，否则 RLS 下零行可见——见 `docs/storage.md`）。
    """
    q = _audit_query(session, tenant_id=tenant_id, action=action, outcome=outcome,
                     actor=actor, since=since, until=until)
    q = q.order_by(M.AuditLogRow.ts.desc(), M.AuditLogRow.audit_id)
    return _page(q, limit, offset)


def count_audit(session: Session, *, tenant_id: str | None,
                action: str | None = None, outcome: str | None = None,
                actor: str | None = None, since: float | None = None,
                until: float | None = None) -> int:
    """与 `list_audit` 同一套过滤条件的计数（供分页 `total`/`has_more`）。"""
    q = _audit_query(session, tenant_id=tenant_id, action=action, outcome=outcome,
                     actor=actor, since=since, until=until)
    return int(q.count())
