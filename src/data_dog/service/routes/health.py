"""`health` 域端点：从 create_app 内联定义按域拆出。

函数体一字未改，只把 `@router.x` 换成 `@router.x`；装配在 service/app.py。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from ... import settings
from ...core import metrics
from ...storage import migrations
from ...storage import models as M
from ...storage import repository as repo
from ...storage.db import session_scope
from ..runtime import (
    _STARTED_AT,
    _VERSION,
    _db_reachable,
    _get_job_runner,
    _log,
    _state,
    _stream_handle,
)

router = APIRouter()


@router.get("/healthz", tags=["probe"])
def healthz() -> dict[str, Any]:
    """存活探针：进程活着即 200，不触碰任何外部依赖。"""
    return {"status": "ok", "service": settings.service_name(),
            "version": _VERSION, "uptime_s": round(time.time() - _STARTED_AT, 1)}
@router.get("/readyz", tags=["probe"])
def readyz() -> JSONResponse:
    """就绪探针：探测数据库连通性。不通 → 503（编排层摘流量，但不重启）。"""
    checks: dict[str, Any] = {"database": _db_reachable()}
    ready = bool(checks["database"]["ok"])

    # 迁移版本（库 schema 是否已到 head）
    try:
        checks["migration"] = {"ok": True,
                               "revision": migrations.current_revision()}
    except Exception as e:                        # noqa: BLE001
        checks["migration"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 算子绑定（缺厂商文档时降级；只影响接入，不影响服务基本可用）
    if _state.resolver is None:
        checks["resolver"] = {"ok": False, "error": _state.build_error}
    else:
        checks["resolver"] = {"ok": True}

    # 流式接入（仅在启用时检查）：启用了但消费线程已死 = 数据静默停收，
    # 必须报未就绪——否则"看起来在跑、其实没在收"是最危险的静默故障。
    if settings.stream_enabled():
        runner, source, thread, _stop = _stream_handle()
        if runner is None:
            checks["stream"] = {"ok": False, "error": "已启用但未启动"}
        elif not thread.is_alive():
            checks["stream"] = {"ok": False, "error": "消费线程已退出"}
        else:
            checks["stream"] = {"ok": True, "source": source.name,
                                "coordination": source.coordination(),
                                "stats": runner.stats}
            # file 源：展示租约归属（一眼看出哪个副本在读）
            holder = getattr(source, "lease_holder", None)
            if holder is not None:
                checks["stream"]["lease_holder"] = holder
        ready = ready and bool(checks["stream"]["ok"])

    # 异步调查 worker 池（仅在启用时检查）：配了异步但池子没跑 = 提交会 503，
    # 报未就绪，让编排层把该实例摘出（实例接请求也只会拒）。
    if settings.investigate_async():
        runner = _get_job_runner()
        if runner is None or not runner.alive:
            checks["investigation_workers"] = {"ok": False,
                                               "error": "异步调查 worker 池未运行"}
        else:
            checks["investigation_workers"] = {"ok": True,
                                               "queue_depth": runner.queue_depth()}
        ready = ready and bool(checks["investigation_workers"]["ok"])

    body = {"status": "ready" if ready else "not_ready", "checks": checks}
    return JSONResponse(status_code=200 if ready else 503, content=body)
@router.get("/metrics", tags=["probe"])
def metrics_endpoint() -> Response:
    """Prometheus 文本格式指标（可被 Prometheus 直接抓取）。

    库内行数用 `COUNT(*)` 聚合实时采集（不做全表加载）；采集失败只记
    日志，不让指标端点整体 500。
    """
    # 进程信息（每次抓取时刷新）
    metrics.info.labels(version=_VERSION).set(1)
    metrics.auth_enabled.set(1 if settings.api_key() else 0)
    metrics.resolver_ready.set(1 if _state.resolver is not None else 0)
    metrics.uptime_seconds.set(round(time.time() - _STARTED_AT, 1))
    _runner, _src, _thr, _stop = _stream_handle()
    metrics.stream_up.set(1 if (_thr is not None and _thr.is_alive()) else 0)
    # 异步调查：队列深度（共享队列里的待办数）+ 各状态 job 数
    jr = _get_job_runner()
    if jr is not None:
        depth = jr.queue_depth()
        if depth >= 0:
            metrics.investigation_queue_depth.set(depth)
    try:
        with session_scope() as s:
            metrics.db_rows.labels(table="candidates").set(
                repo.count_rows(s, M.CandidateRow))
            metrics.db_rows.labels(table="alerts").set(
                repo.count_rows(s, M.AlertRow))
            metrics.db_rows.labels(table="outbox_pending").set(
                repo.count_rows(s, M.AlertOutboxRow, status="pending"))
            metrics.db_rows.labels(table="investigation_jobs_pending").set(
                repo.count_rows(s, M.InvestigationJobRow, status="pending"))
            metrics.db_rows.labels(table="investigation_jobs_running").set(
                repo.count_rows(s, M.InvestigationJobRow, status="running"))
    except Exception as e:                        # noqa: BLE001
        _log.warning("采集库内行数失败", extra={
            "error": f"{type(e).__name__}: {e}"})
    return Response(content=metrics.render(),
                    media_type="text/plain; version=0.0.4; charset=utf-8")
