"""进程内共享运行时状态与助手（ServiceState / 流 / 作业 / 检查点 / 升级 / 调查执行）。端点模块从这里取共享状态，避免 routes→app 的循环依赖。"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

from fastapi import HTTPException, Response

from .. import settings
from ..agent.loop import AgentLoop
from ..alerting.saas import SaaSTestAdapter
from ..core import metrics
from ..core.logging import get_logger
from ..core.redact import redact
from ..data_plane.candidate_shared import SharedCandidateBuilder
from ..data_plane.gateway import TelemetryGateway
from ..data_plane.p2_demo import _detector_config
from ..data_plane.pipeline import DataPlane, build_demo_resolver
from ..errors import ToolErrorCode, http_status_for
from ..presentation import derive_verdict
from ..providers.client import LLMCallError
from ..providers.config import load_config
from ..providers.offline_demo import DeterministicEvidenceProvider
from ..schemas.events import CandidateEvent, EvidenceSummary
from ..state import get_state_store
from ..storage import repository as repo
from ..storage.db import database_url as _database_url
from ..storage.db import get_engine, session_scope
from ..tools.investigation import InvestigationTools
from .alerting import notify_alert, report_alert
from .evidence import RepoEvidenceBridge

_log = get_logger(__name__)
def _route_template(request) -> str:
    """取**路由模板**作为指标标签，而非实际 URL。

    实际 URL 带查询串/路径参数会把标签基数撑爆（Prometheus 大忌，
    每个不同 URL 都会生成一条新时间序列）。优先用 FastAPI 匹配到的
    路由模板（如 `/v1/candidates`），取不到则退回 `unmatched`。
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or "unmatched"
_VERSION = "0.1.0"
_STARTED_AT = time.time()
class ServiceState:
    """进程内状态：构建一次解析器/绑定/网关，复用到每个请求。

    - resolver/绑定是无状态只读的，构建一次即可。
    - **gateway 必须跨请求共享**：它的令牌桶是限流状态的载体（§16.3）。若每个
      请求新建 gateway，桶会随请求生灭、永远满桶，跨请求限流形同虚设。
    - 每个请求仍新建一个 DataPlane：候选合并器(CandidateBuilder)与窗口缓冲都是
      有状态累积器，跨请求复用会导致候选无限增长且把不同时间的命中错误合并，
      因此按请求隔离（见 docs/delivery.md 的说明）。DataPlane 通过注入共享
      gateway 获得跨请求限流，同时保持自身的窗口/合并器隔离。
    """

    def __init__(self) -> None:
        self.resolver = None
        self.build_error: str | None = None
        self.request_count = 0
        self.rejected_requests = 0
        # 跨请求共享的接入网关（限流状态所在）
        self.gateway = TelemetryGateway(
            rate_per_sec=settings.rate_per_sec(),
            burst=settings.rate_burst(),
        )
        # P3 上报的投递目标（本地测试 SaaS）。进程级共享：其幂等去重表
        # (`_seen`) 跨请求有效，重复投递同一 (key, revision) 只产生一次外部
        # 效果。事件/outbox 的**持久真相**在 DB，这里只是投递端。
        self.saas_adapter = SaaSTestAdapter()
        # 水位线（G2）：开启时**进程级共享**——流式长驻 plane 让所有消息推进同一条
        # 水位线，而不是每请求/每 flush 重置。关闭时为 None（旧行为，零回归）。
        self.watermark: Any = None
        if settings.watermark_enabled():
            from ..data_plane.watermark import Watermark
            self.watermark = Watermark(
                allowed_lateness_s=settings.allowed_lateness_s())
        try:
            self.resolver = build_demo_resolver(_detector_config())
        except Exception as e:            # 缺厂商文档/依赖时不致命，降级为不可用
            self.build_error = f"{type(e).__name__}: {e}"

    def new_plane(self) -> DataPlane | None:
        if self.resolver is None:
            return None
        # 候选构建器走**共享后端**：跨副本/跨请求一致地归并同一设备的命中。
        # 单副本（memory 后端）行为等价；多副本（PG/Redis）才真正共享。
        # 每次请求新建实例，但分组状态在共享存储里，故跨请求仍能正确合并。
        builder = SharedCandidateBuilder(state_store=get_state_store(),
                                         watermark=self.watermark)
        return DataPlane(self.resolver, gateway=self.gateway, builder=builder,
                         watermark=self.watermark)
_state = ServiceState()
_stream_lock = threading.Lock()
_stream: tuple[Any, Any, threading.Thread, threading.Event] | None = None
def _set_stream(runner, source, thread, stop) -> None:
    global _stream
    with _stream_lock:
        _stream = (runner, source, thread, stop)
def _stream_handle():
    with _stream_lock:
        return _stream if _stream is not None else (None, None, None, None)
def _clear_stream() -> None:
    global _stream
    with _stream_lock:
        _stream = None
_job_runner_lock = threading.Lock()
_job_runner: Any = None
def _get_job_runner():
    with _job_runner_lock:
        return _job_runner
def _set_job_runner(runner) -> None:
    global _job_runner
    with _job_runner_lock:
        _job_runner = runner
def _start_job_runner_if_needed() -> None:
    """起异步调查 worker 池。

    只在 `DD_INVESTIGATE_ASYNC=1` 时启动——否则 `POST /v1/investigations` 走同步
    路径，无需 worker。worker 的 run_fn 注入 `_execute_investigation`（本模块内
    唯一的调查实现），故同步/异步两条路径**共用同一段执行逻辑**。
    """
    if not settings.investigate_async():
        return
    from .investigation_jobs import JobRunner, SharedJobQueue

    runner = JobRunner(
        _run_investigation_job,
        workers=settings.investigate_workers(),
        queue=SharedJobQueue(),
        poll_interval_s=settings.investigate_queue_poll_ms() / 1000.0,
    )
    runner.start()
    _set_job_runner(runner)
def _stop_job_runner() -> None:
    runner = _get_job_runner()
    if runner is None:
        return
    runner.stop()
    _set_job_runner(None)
_checkpoint_lock = threading.Lock()
_checkpoint_loop: Any = None
def _get_checkpoint_loop():
    with _checkpoint_lock:
        return _checkpoint_loop
def _set_checkpoint_loop(loop) -> None:
    global _checkpoint_loop
    with _checkpoint_lock:
        _checkpoint_loop = loop
def _start_checkpoint_if_enabled() -> None:
    """起周期状态快照任务（`DD_CHECKPOINT_ENABLED=1`）。

    可选：先尝试 `DD_CHECKPOINT_RESTORE=1` 从最新快照恢复（覆盖=否，只补缺失），
    再起周期导出。状态后端不可用时静默跳过（不影响 HTTP 服务）。
    """
    if not settings.checkpoint_enabled():
        return
    from ..state import get_state_store
    from ..state.checkpoint import CheckpointLoop, restore_latest

    store = get_state_store()
    if settings.checkpoint_restore():
        restore_latest(store, overwrite=False)
    loop = CheckpointLoop(store, interval_s=settings.checkpoint_interval_s(),
                          keep=settings.checkpoint_keep(),
                          prefix=settings.checkpoint_prefix())
    loop.start()
    _set_checkpoint_loop(loop)
def _stop_checkpoint() -> None:
    loop = _get_checkpoint_loop()
    if loop is None:
        return
    loop.stop()
    _set_checkpoint_loop(None)
_escalation_lock = threading.Lock()
_escalation: Any = None
def _get_escalation():
    with _escalation_lock:
        return _escalation
def _set_escalation(sweeper) -> None:
    global _escalation
    with _escalation_lock:
        _escalation = sweeper
def _start_escalation_if_enabled() -> None:
    """起告警升级 sweeper（`DD_NOTIFY_ENABLED=1`）。

    默认关：不配 `DD_NOTIFY_ENABLED` 时告警流止于 SaaS adapter（落库审计），不主动
    外发、不升级。开启后才按值班表派发并计时升级。
    """
    if not settings.notify_enabled():
        return
    from ..alerting.escalation import EscalationEngine, EscalationSweeper

    sweeper = EscalationSweeper(EscalationEngine())
    sweeper.start()
    _set_escalation(sweeper)
    _log.info("告警升级 sweeper 已启动", extra={
        "interval_s": settings.notify_sweep_interval_s(),
        "lease_ttl_s": settings.notify_sweep_lease_ttl_s()})
def _stop_escalation() -> None:
    sweeper = _get_escalation()
    if sweeper is None:
        return
    sweeper.stop()
    _set_escalation(None)
def _run_investigation_job(tenant_id: str, candidate_id: str,
                           should_cancel) -> dict[str, Any]:
    """worker 的执行回调：跑一次调查，返回 job 终态结果（不抛异常给 worker）。

    返回 `{"status", "report_id", "error_code"}`：
      - 成功 → succeeded + report_id
      - 取消 → cancelled（由 worker 侧 finish_job 拒绝覆盖，这里如实返回 cancelled）
      - 失败 → failed + 有限错误码
    """
    started = time.monotonic()
    status: str = "failed"
    report_id: str | None = None
    error_code: str | None = "execution_failed"
    report: Any = None
    run_log: Any = None
    try:
        # persist=False：报告不在这里落库（否则与 job 终态是两个事务），
        # 交给 JobRunner._finish 与终态同事务写。
        outcome = _execute_investigation(
            tenant_id, candidate_id, should_cancel=should_cancel, persist=False)
        # 取消：provider 抛 _InvestigationCancelled，_execute_investigation 会
        # 把它翻译成 status="cancelled"（不产出报告）。
        status = outcome.get("status", "failed")
        report_id = outcome.get("report_id")
        error_code = outcome.get("error_code")
        report = outcome.get("report")
        run_log = outcome.get("run_log")
    except Exception as e:                            # noqa: BLE001
        _log.error("异步调查执行异常", extra={
            "candidate_id": candidate_id,
            "error": f"{type(e).__name__}: {e}"})
        error_code = "execution_failed"
    finally:
        metrics.investigation_job_duration_seconds.observe(time.monotonic() - started)
        metrics.investigation_jobs_total.labels(status=status).inc()
    return {"status": status, "report_id": report_id, "error_code": error_code,
            "report": report, "candidate_id": candidate_id, "run_log": run_log}
def _db_reachable() -> dict[str, Any]:
    """探测数据库连通性：直接建立一条连接（拿到即通，不做任何查询）。"""
    try:
        conn = get_engine().connect()
        conn.close()
        return {"ok": True, "url_scheme": _url_scheme()}
    except Exception as e:                        # noqa: BLE001
        # 公开探针：DB 异常消息可能带 DSN/口令 → 脱敏后再回给编排层
        return {"ok": False, "error": redact(f"{type(e).__name__}: {e}")}
_PUBLIC_PATHS = frozenset({"/healthz", "/readyz"})
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 500
def _url_scheme() -> str:
    url = _database_url()
    return url.split(":", 1)[0] if ":" in url else url
def _stats_dict(result: Any) -> dict[str, Any]:
    st = result.stats
    return {
        "ingested": st.ingested,
        "resolved": st.resolved,
        "rejected": st.rejected,
        "rejected_by_reason": dict(st.rejected_by_reason),
        "normalized": len(result.normalized),
        "candidates": len(result.candidates),
    }
def _row_to_candidate(row: dict[str, Any]) -> CandidateEvent:
    """把落库的候选行重建为 `CandidateEvent`（Agent 调查的输入）。"""
    evidence = []
    for e in row.get("evidence", []) or []:
        if isinstance(e, dict):
            evidence.append(EvidenceSummary(
                key=e.get("key", ""),
                from_value=e.get("from_value"),
                to_value=e.get("to_value"),
                baseline=e.get("baseline"),
                unit=e.get("unit"),
                note=e.get("note"),
            ))
        else:
            evidence.append(e)
    return CandidateEvent(
        candidate_id=row["candidate_id"],
        device_sn=row["device_sn"],
        component_ref=row["component_ref"],
        signal=row["signal"],
        severity_hint=row["severity_hint"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        sample_count=int(row.get("sample_count", 0)),
        rule_matches=list(row.get("rule_matches", [])),
        anomaly_score=float(row.get("anomaly_score", 0.0)),
        evidence=evidence,
        evidence_refs=list(row.get("evidence_refs", [])),
        detector_provenance=row.get("detector_provenance", ""),
    )
def _job_public(job: dict[str, Any]) -> dict[str, Any]:
    """job 行的对外视图（去掉内部 worker_id，保留可轮询的字段）。"""
    return {
        "job_id": job["job_id"],
        "candidate_id": job["candidate_id"],
        "status": job["status"],
        "report_id": job.get("report_id"),
        "error_code": job.get("error_code"),
        "submitted_at": job.get("submitted_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }
def _run_log_public(row: dict[str, Any], *, with_steps: bool = True) -> dict[str, Any]:
    """运行日志的对外视图。**列表页不带 steps 正文**（一次 40 步轨迹不该塞进列表响应），
    详情页带；两者共用同一份字段口径，避免"列表和详情说的不是一回事"。"""
    out: dict[str, Any] = {
        "run_id": row.get("run_id"),
        "candidate_id": row.get("candidate_id"),
        "kind": row.get("kind"),
        "status": row.get("status"),
        "report_id": row.get("report_id"),
        "error_code": row.get("error_code"),
        "error_detail": row.get("error_detail") or "",
        "model": row.get("model") or "",
        "step_count": row.get("step_count", 0),
        "duration_ms": row.get("duration_ms", 0.0),
        "usage": row.get("usage") or {},
        "created_at": row.get("created_at"),
    }
    if with_steps:
        out["steps"] = row.get("steps") or []
    return out
def _submit_async_investigation(tenant: str, candidate_id: str,
                                response: Response) -> dict[str, Any]:
    """提交异步调查：校验候选存在 → 落 pending job → 入共享队列 → 202 返回句柄。

    **立即返回**（不跑 LLM）：这是把 worker 从长调用里解放出来的关键。
    候选不存在 → 404（与同步路径一致的失败语义）。
    """
    with session_scope() as s:
        row = repo.get_candidate(s, candidate_id, tenant_id=tenant)
    if row is None:
        metrics.investigations_total.labels(result="not_found").inc()
        raise HTTPException(status_code=404, detail="候选不存在")

    runner = _get_job_runner()
    if runner is None or not runner.alive:
        # 配了 DD_INVESTIGATE_ASYNC 但 worker 池没起来：失败关闭，不假装受理。
        raise HTTPException(status_code=503,
                            detail="异步调查 worker 池未运行（检查启动日志）")

    job_id = _make_job_id(tenant, candidate_id)
    with session_scope() as s:
        repo.create_job(s, tenant_id=tenant, job_id=job_id,
                        candidate_id=candidate_id)
    runner.submit(tenant_id=tenant, candidate_id=candidate_id, job_id=job_id)
    metrics.investigation_jobs_total.labels(status="pending").inc()

    response.status_code = 202
    return {"ok": True, "job_id": job_id, "status": "pending",
            "candidate_id": candidate_id,
            "poll": f"/v1/investigations/jobs/{job_id}"}
_PRIOR_REPORT_SCAN_LIMIT = 200


def _prior_report_items(session, tenant: str, *, device_sn: str,
                        component_ref: str, exclude_candidate: str = "",
                        limit: int = 5) -> list[dict[str, Any]]:
    """取该设备/部件的既往调查报告，供 Agent 跨调查记忆（先验）。

    MVP 边界：报告行本身**不含 device/component**（只有 candidate_refs），故这里先按
    (device, component) 取候选建立映射，再在租户**最近 N 条报告**里内存过滤。
    规模化时该给 reports 加 (device_sn, component_ref) 列 + 索引，直接 SQL 过滤。
    """
    cands = repo.list_candidates(session, tenant_id=tenant, device_sn=device_sn,
                                 component_ref=component_ref)
    meta: dict[str, tuple[str, str]] = {
        c["candidate_id"]: (c.get("component_ref", ""), c.get("signal", ""))
        for c in cands
    }
    if not meta:
        return []
    rows = repo.list_reports(session, tenant_id=tenant, limit=_PRIOR_REPORT_SCAN_LIMIT)
    out: list[dict[str, Any]] = []
    for r in rows:
        hit = sorted(set(r.get("candidate_refs") or []) & set(meta))
        if not hit:
            continue
        cid = hit[0]
        if cid == exclude_candidate:
            continue
        comp, sig = meta.get(cid, (component_ref, ""))
        out.append({
            "investigation_id": r.get("investigation_id", ""),
            "candidate_id": cid,
            "component_ref": comp,
            "signal": sig,
            "disposition": r.get("disposition", ""),
            "severity": r.get("severity"),
            "confidence": r.get("confidence"),
            "event_type": r.get("event_type"),
            "summary": (r.get("summary") or "")[:200],
            "created_at": r.get("created_at", ""),
            "counter_evidence_refs": list(r.get("counter_evidence_refs") or [])[:5],
        })
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return out[:limit]


# ---------------------------------------------------------------- 运行日志（回看用）
# 目的：技术人员**事后**能调出"这次调查到底做了什么、为什么失败"。
# 三条纪律：① 失败也要落（那才是最需要看的）；② 落库前脱敏 + 裁剪（日志不是证据搬运工）；
# ③ 落库失败**绝不能**把调查结果一起毁掉（只告警）。
_RUN_LOG_MAX_STEPS = 40          # 轨迹最多留多少步（AgentLoop 本身有 max_steps 上限）
_RUN_LOG_MAX_STEP_CHARS = 4000   # 单步（入参 + 结果）序列化后的字符上限
_RUN_LOG_MAX_DETAIL = 1000       # 失败上下文上限


def _safe_step(ev: dict[str, Any]) -> dict[str, Any]:
    """把一步轨迹脱敏 + 裁剪成可落库的 JSON；超长**如实标注**截断，不假装完整。"""
    blob = json.dumps({"step": ev.get("step"), "name": ev.get("name"),
                       "args": ev.get("args"), "result": ev.get("result")},
                      ensure_ascii=False, default=str)
    blob = redact(blob, max_len=_RUN_LOG_MAX_STEP_CHARS)
    try:
        out = json.loads(blob)
    except Exception:                                 # noqa: BLE001
        return {"step": ev.get("step"), "name": str(ev.get("name", "")), "_raw": blob}
    if isinstance(out, dict) and len(json.dumps(out, ensure_ascii=False)) <= 20:
        # redact 触发过截断（原 JSON 被砍成短串）→ 无从解析，退回原文
        return {"step": ev.get("step"), "name": str(ev.get("name", "")), "_raw": blob}
    return out


def _run_log_payload(*, run_id: str, candidate_id: str, kind: str, status: str,
                     trace: list[dict[str, Any]], report_id: str | None = None,
                     error_code: str | None = None, error_detail: str = "",
                     usage: dict[str, Any] | None = None, model: str = "",
                     duration_ms: float = 0.0) -> dict[str, Any]:
    """构造一条运行日志。落库与响应**共用同一份口径**，避免两处各写一遍而漂移。"""
    steps = [_safe_step(ev) for ev in trace[:_RUN_LOG_MAX_STEPS]]
    return {
        "run_id": run_id,
        "candidate_id": candidate_id,
        "kind": kind,
        "status": status,
        "report_id": report_id,
        "error_code": error_code,
        "error_detail": redact(error_detail, max_len=_RUN_LOG_MAX_DETAIL) if error_detail else "",
        "model": model,
        "step_count": len(trace),
        "duration_ms": round(float(duration_ms), 1),
        "steps": steps,
        "usage": usage or {},
    }


def _persist_run_log(tenant: str, log: dict[str, Any]) -> None:
    """同步路径自己落库：**失败只告警不抛出**——日志写不进去不该毁掉调查结果。"""
    try:
        with session_scope(tenant_id=tenant) as s:
            repo.save_run_log(s, log, tenant_id=tenant)
    except Exception as e:                            # noqa: BLE001
        _log.warning("运行日志落库失败",
                     extra={"run_id": log.get("run_id"),
                            "error": redact(f"{type(e).__name__}: {e}")})


def _execute_investigation(tenant: str, candidate_id: str, *,
                           should_cancel=None, persist: bool = True) -> dict[str, Any]:
    """一次调查的**唯一实现**（同步端点与异步 worker 共用）。

    返回统一结构，两条路径各自翻译：
      - 同步端点看 `ok`/`http_status`/`detail`/`response`。
      - worker 看 `status`（succeeded/failed/cancelled）/`report_id`/`error_code`/`report`。

    `persist=False`（worker 路径）：**不落库报告**，而是把报告对象放进 `report` 返回，
    由调用方与 job 终态在**同一个事务**里写——这样"job 成功 ⇔ 报告存在"才成立。

    失败/取消**不抛异常**（除取消用哨兵外），全部收敛成返回值——这样 worker 不必
    区分异常种类，同步端点也能稳定映射 HTTP 状态。

    **每一个出口都挂运行日志**（见 _run_log_payload）：成功挂着，失败/取消照样挂——
    "为什么没查出结果"恰恰是最需要事后看的。同步路径当场落库；异步路径把日志塞进返回值，
    由 JobRunner._finish 与 job 终态**同事务**写（"job 有终态 ⇔ 有运行日志"）。
    """
    trace: list[dict[str, Any]] = []
    started = time.monotonic()
    attempt_run_id = f"run_{uuid.uuid4().hex[:16]}"

    def _with_log(out: dict[str, Any], *, status: str, run_id: str | None = None,
                  report_id: str | None = None, error_code: str | None = None,
                  error_detail: str = "", usage: dict[str, Any] | None = None
                  ) -> dict[str, Any]:
        log = _run_log_payload(
            run_id=run_id or attempt_run_id, candidate_id=candidate_id,
            kind="sync" if persist else "async", status=status, trace=trace,
            report_id=report_id, error_code=error_code, error_detail=error_detail,
            usage=usage, model=str(settings.llm_model() or ""),
            duration_ms=(time.monotonic() - started) * 1000.0)
        out["run_id"] = log["run_id"]
        out["run_log"] = log
        if persist:
            _persist_run_log(tenant, log)
        return out

    with session_scope() as s:
        row = repo.get_candidate(s, candidate_id, tenant_id=tenant)
        if row is None:
            metrics.investigations_total.labels(result="not_found").inc()
            return _with_log({"ok": False, "http_status": 404, "detail": "候选不存在",
                              "status": "failed", "error_code": "not_found"},
                             status="failed", error_code="not_found")
        tel_rows = repo.list_telemetry(s, tenant_id=tenant,
                                       device_sn=row["device_sn"])
        # 跨调查记忆（先验）：该设备/部件的既往报告（排除本次候选自己）
        prior_items = _prior_report_items(
            s, tenant, device_sn=row["device_sn"],
            component_ref=row["component_ref"], exclude_candidate=candidate_id)

    cand = _row_to_candidate(row)

    try:
        provider = _build_investigation_provider(should_cancel=should_cancel)
    except HTTPException as e:
        metrics.investigations_total.labels(result="failed").inc()
        return _with_log({"ok": False, "http_status": e.status_code, "detail": e.detail,
                          "status": "failed", "error_code": "unavailable"},
                         status="failed", error_code="unavailable",
                         error_detail=str(e.detail))

    try:
        bridge = RepoEvidenceBridge(tel_rows, device_sn=cand.device_sn,
                                    prior_reports=prior_items)
        # 无损视觉记忆（VISTA-1 式）：设了目录才启用；Pillow 缺失则工具不暴露（零回归）
        visual = None
        vdir = settings.visual_evidence_dir()
        if vdir:
            from ..evidence.visual import VisualFrameStore
            visual = VisualFrameStore(vdir)
            if not visual.available:
                _log.warning("已设 DD_VISUAL_EVIDENCE_DIR 但 Pillow 不可用，视觉工具不暴露",
                             extra={"dir": vdir})
        tools = InvestigationTools(bridge, visual=visual)
        agent = AgentLoop(provider, tools, model=settings.llm_model() or None,
                          max_steps=settings.investigate_max_steps(),
                          trace_cb=trace.append)
        report = agent.investigate(cand)
    except _InvestigationCancelled:
        # 协作式取消：不产出报告（要的就是"停，且不留半成品结论"）
        metrics.investigations_total.labels(result="cancelled").inc()
        return _with_log({"ok": False, "http_status": 409, "detail": "调查已被取消",
                          "status": "cancelled", "error_code": "cancelled"},
                         status="cancelled", error_code="cancelled")
    except LLMCallError as e:
        # 超时/不可用 → 统一错误码（504/503），失败关闭，不假装查过证据
        metrics.investigations_total.labels(result="timeout").inc()
        return _with_log({"ok": False, "http_status": http_status_for(e.code),
                          "detail": e.detail, "status": "failed",
                          "error_code": e.code.value},
                         status="failed", error_code=e.code.value,
                         error_detail=str(e.detail))
    except Exception as e:                          # noqa: BLE001
        metrics.investigations_total.labels(result="failed").inc()
        return _with_log({"ok": False, "http_status": 500,
                          "detail": f"调查执行失败：{type(e).__name__}",
                          "status": "failed", "error_code": "execution_failed"},
                         status="failed", error_code="execution_failed",
                         error_detail=f"{type(e).__name__}: {e}")

    metrics.investigations_total.labels(result="ok").inc()

    # 持久化报告（幂等 upsert）。
    # `persist=False`（异步 worker 路径）：**不在这里落库**，把报告对象交给调用方，
    # 由它与 job 终态在**同一个事务**里写——否则会出现"job=succeeded 但报告查不到"
    # （此前正是如此：这里单独提交、异常还被吞掉，job 照样标成功）。
    report_persisted = False
    if persist:
        try:
            with session_scope() as s:
                repo.save_report(s, report, tenant_id=tenant)
            report_persisted = True
        except Exception as e:                      # noqa: BLE001
            # 落库失败不吞掉已算出的结论（同步端点把报告原样返回给调用方），
            # 但**如实标明**：响应里带 `report_persisted=False`，别让调用方以为存住了。
            _log.warning("报告落库失败", extra={"error": f"{type(e).__name__}: {e}"})

    # ---- P3：策略门 → 正式事件 → outbox（全部落库）----
    # 这一步让 /v1/alerts、/v1/feedback、/v1/summary 的 alerts_by_state 在真实
    # 运行里**有数据**——此前后端从不产告警，这些端点是悬空的。失败不吞掉已
    # 算出的结论，但如实回报（alerting 字段标 reported=False + 原因）。
    alerting_result: dict[str, Any] = {"reported": False, "reason": "未执行"}
    try:
        with session_scope() as s:
            alerting_result = report_alert(
                s, tenant_id=tenant, cand=cand, report=report.to_dict(),
                adapter=_state.saas_adapter)
    except Exception as e:                          # noqa: BLE001
        _log.warning("P3 上报失败", extra={"error": f"{type(e).__name__}: {e}"})
        alerting_result = {"reported": False,
                           "reason": redact(f"{type(e).__name__}: {e}")}

    # ---- P4-3：按值班表派发通知（默认关；无值班人则失败关闭）----
    # 在告警落库事务**提交之后**（独立会话）做，避免与落库事务争同一连接锁。
    if alerting_result.get("reported") and alerting_result.get("new"):
        try:
            alerting_result["dispatch"] = notify_alert(
                tenant, str(alerting_result.get("event_id", "")))
        except Exception as e:                      # noqa: BLE001
            _log.warning("P4-3 通知派发失败",
                         extra={"error": f"{type(e).__name__}: {e}"})
            alerting_result["dispatch"] = {"dispatched": False,
                                           "reason": redact(f"{type(e).__name__}: {e}")}

    report_dict = report.to_dict()
    usage = report_dict.get("model_provenance", {}).get("usage", {})
    # 人话结论：与演示台/候选详情同源（presentation.derive_verdict）
    tel_map = {cand.component_ref: {
        "metrics": {},
        "device_sn": cand.device_sn,
    }}
    # 从 trace 里取该候选窗口的实测指标，给 verdict 用（与证据端点一致）
    for step in trace:
        res = step.get("result") or {}
        if step.get("name") == "query_candidate_evidence" and res.get("metrics"):
            tel_map[cand.component_ref]["metrics"] = res["metrics"]
            break
    verdict, verdict_detail = derive_verdict(
        [report_dict | {"severity_hint": cand.severity_hint,
                        "component_ref": cand.component_ref,
                        "rule_matches": cand.rule_matches}],
        {}, tel_map)
    response = {
        "ok": True,
        "investigation_id": report.investigation_id,
        "candidate_id": candidate_id,
        "report": report_dict,
        "verdict": verdict,
        "verdict_detail": verdict_detail,
        "trace": trace,
        "usage": usage,
        "alerting": alerting_result,
        # 落库是否成功**如实回报**（同步路径下报告已随响应返回给调用方，但库里可能没有）
        "report_persisted": report_persisted,
    }
    out: dict[str, Any] = {"ok": True, "response": response, "status": "succeeded",
                           "report_id": report.investigation_id, "error_code": None}
    if not persist:
        # 异步 worker：报告交给它，与 job 终态同事务落库（见 investigation_jobs._finish）
        out["report"] = report
    # 同步成功：run_id 直接用 investigation_id——技术员手上是报告 id，就能直接取日志。
    # 同步失败没有报告，run_id 是本次尝试的 run_xxx，靠 GET /v1/investigations/runs 发现。
    result = _with_log(out, status="succeeded",
                       run_id=report.investigation_id if persist else None,
                       report_id=report.investigation_id, usage=usage)
    # 同步端点只回 response（out 是给 worker 看的），故把 run_id 也放进 response，
    # 调用方拿到报告 id 的**同时**就知道运行日志去哪取。
    response["run_id"] = result["run_id"]
    return result
def _make_job_id(tenant_id: str, candidate_id: str) -> str:
    from .investigation_jobs import make_job_id
    return make_job_id(tenant_id, candidate_id)
class _InvestigationCancelled(Exception):
    """协作式取消信号：worker 在 provider 调用边界发现 job 已被取消。"""
class _DeadlineProvider:
    """给 provider 加一个**整次调查的总墙钟预算**（可选叠加协作式取消）。

    单次 LLM 调用超时（`Provider.timeout`）只保证"一次调用不会永久挂起"；但一次
    调查最多 `max_steps` 次调用，若每次都快到超时、叠加起来仍可能远超契约承诺的
    90s。本包装器把"整次调查"纳入同一个预算，到点后任何后续调用立即以
    `LLMCallError(TIMEOUT)` 失败关闭——端点因此有确定上界，不会长期占用 worker。
    `AgentLoop` 捕获该异常后会产出 fail-closed 报告（不假装查过证据）。

    `should_cancel`（G1 异步取消）：每次调用前检查一次，为真则抛
    `_InvestigationCancelled`——它**不被 AgentLoop 捕获**（与 TIMEOUT 不同），
    故取消时不产出报告。这正是取消想要的："停，且不留半成品结论"。
    """

    def __init__(self, inner: Any, budget_s: float, should_cancel=None):
        self._inner = inner
        self._deadline = time.monotonic() + max(0.0, budget_s)
        self._budget_s = budget_s
        self._should_cancel = should_cancel

    def chat(self, messages, **kw):
        if self._should_cancel is not None and self._should_cancel():
            raise _InvestigationCancelled()
        if time.monotonic() >= self._deadline:
            raise LLMCallError(ToolErrorCode.TIMEOUT,
                               f"调查总预算 {self._budget_s:.0f}s 已耗尽")
        return self._inner.chat(messages, **kw)
def _build_investigation_provider(should_cancel=None) -> Any:
    """按配置构造调查用的 provider（每次调用新建，保证状态隔离）。

    - `DD_INVESTIGATE_OFFLINE=1` → 离线确定性 provider（CI/测试不调真实模型，
      结论仍由证据推导，非写死剧本）。
    - 否则用已配置的供应商 + key；未配置 → 503（不静默降级成假结论）。
    """
    if settings.investigate_offline():
        return _DeadlineProvider(DeterministicEvidenceProvider(),
                                 settings.investigate_timeout_s(),
                                 should_cancel=should_cancel)

    cfg = load_config()
    key = (cfg.get("api_key") or "").strip()
    if not key:
        raise HTTPException(
            status_code=503,
            detail="未配置 LLM 凭据（先 data-dog init 设 key，"
                   "或设 DD_INVESTIGATE_OFFLINE=1 用离线确定性 provider）")
    from ..providers.client import Provider

    provider = Provider.from_config(
        cfg.get("vendor_id") or settings.llm_vendor(), key,
        timeout=settings.investigate_timeout_s(),
        base_url=cfg.get("base_url") or settings.llm_base_url(),
    )
    return _DeadlineProvider(provider, settings.investigate_timeout_s(),
                             should_cancel=should_cancel)
