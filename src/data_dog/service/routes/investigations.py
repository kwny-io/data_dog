"""`investigations` 域端点：从 create_app 内联定义按域拆出。

函数体一字未改，只把 `@router.x` 换成 `@router.x`；装配在 service/app.py。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ... import settings
from ...storage import models as M
from ...storage import repository as repo
from ...storage.db import session_scope
from ..deps import require_tenant
from ..runtime import (
    _DEFAULT_PAGE_SIZE,
    _MAX_PAGE_SIZE,
    _execute_investigation,
    _job_public,
    _run_log_public,
    _submit_async_investigation,
)

router = APIRouter()


@router.get("/v1/investigations/jobs", tags=["investigate"])
def list_investigation_jobs(
    tenant: str = Depends(require_tenant),
    status: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """分页列出本租户的异步调查 job（可按 status 过滤）。"""
    with session_scope() as s:
        rows = repo.list_jobs(s, tenant_id=tenant, status=status,
                              limit=limit, offset=offset)
        total = repo.count_rows(s, M.InvestigationJobRow, tenant_id=tenant)
    return {"ok": True, "count": len(rows), "total": total,
            "limit": limit, "offset": offset,
            "has_more": offset + len(rows) < total,
            "items": [_job_public(r) for r in rows]}
@router.get("/v1/investigations/jobs/{job_id}", tags=["investigate"])
def get_investigation_job(
    job_id: str,
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """查异步调查 job 的状态（跨租户 → 404）。

    status ∈ pending/running/succeeded/failed/cancelled。succeeded 时 `report_id`
    可拿去 `GET /v1/investigations/{report_id}` 读完整报告。
    """
    with session_scope() as s:
        job = repo.get_job(s, job_id, tenant_id=tenant)
    if job is None:
        raise HTTPException(status_code=404, detail="调查任务不存在")
    return {"ok": True, "job": _job_public(job)}
@router.get("/v1/investigations/jobs/{job_id}/log", tags=["investigate"])
def get_investigation_job_log(
    job_id: str,
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """异步调查的**运行日志**（ReAct 轨迹 + 失败上下文）：run_id 就是 job_id。

    给技术员事后排查用——成功时看"它查了什么、凭什么这么判"，失败时看
    error_code / error_detail（失败也写日志，那才是最需要看的一类）。
    """
    with session_scope() as s:
        log = repo.get_run_log(s, job_id, tenant_id=tenant)
    if log is None:
        raise HTTPException(status_code=404, detail="运行日志不存在")
    return {"ok": True, "log": _run_log_public(log)}


@router.post("/v1/investigations/jobs/{job_id}/cancel", tags=["investigate"])
def cancel_investigation_job(
    job_id: str,
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """请求取消异步调查 job（跨租户 → 404）。

    **协作式**：pending 的直接取消；running 的置 cancelled，worker 在下一个
    provider 调用边界停止（已发出的 LLM 调用要等它返回），故报告可能已落库——
    以 job 的最终状态为准。已终态的任务原样返回，不改。
    """
    with session_scope() as s:
        new_status = repo.cancel_job(s, job_id, tenant_id=tenant)
        job = repo.get_job(s, job_id, tenant_id=tenant)
    if new_status is None or job is None:
        raise HTTPException(status_code=404, detail="调查任务不存在")
    return {"ok": True, "job": _job_public(job)}
@router.get("/v1/investigations/runs", tags=["investigate"])
def list_investigation_runs(
    tenant: str = Depends(require_tenant),
    candidate_id: str | None = Query(default=None,
                                     description="只看某个候选的运行记录"),
    status: str | None = Query(default=None,
                               description="succeeded / failed / cancelled"),
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """列出本租户的**调查运行日志**（新的在前）——"这个候选为什么老是失败"从这下手。

    只回摘要（**不含 steps 正文**），正文用 `GET /v1/investigations/{id}/log` 或
    `GET /v1/investigations/jobs/{job_id}/log` 取。
    """
    with session_scope() as s:
        rows = repo.list_run_logs(s, tenant_id=tenant, candidate_id=candidate_id,
                                  status=status, limit=limit, offset=offset)
        total = repo.count_rows(s, M.InvestigationRunLogRow, tenant_id=tenant)
    return {"ok": True, "count": len(rows), "total": total,
            "limit": limit, "offset": offset,
            "has_more": offset + len(rows) < total,
            "items": [_run_log_public(r, with_steps=False) for r in rows]}


@router.get("/v1/investigations/{investigation_id}", tags=["query"])
def get_investigation(
    investigation_id: str,
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """读已持久化的调查报告（跨租户 → 404）。前端据此免重跑 LLM。"""
    with session_scope() as s:
        report = repo.get_report(s, investigation_id, tenant_id=tenant)
    if report is None:
        raise HTTPException(status_code=404, detail="调查报告不存在")
    return {"ok": True, "investigation_id": investigation_id,
            "report": report}
@router.get("/v1/investigations/{investigation_id}/log", tags=["investigate"])
def get_investigation_log(
    investigation_id: str,
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """**同步**调查的运行日志：run_id 直接用 investigation_id（报告 id），技术员手上
    通常是报告 id，一步到位。

    异步调查的日志挂在 **job_id** 上（执行者才知道 job），用
    `GET /v1/investigations/jobs/{job_id}/log`；同步调用失败时没有报告，run_id 是本次
    尝试的 `run_xxx`，用 `GET /v1/investigations/runs?candidate_id=...&status=failed` 找。
    """
    with session_scope() as s:
        log = repo.get_run_log(s, investigation_id, tenant_id=tenant)
    if log is None:
        raise HTTPException(status_code=404, detail="运行日志不存在")
    return {"ok": True, "log": _run_log_public(log)}


@router.post("/v1/investigations", tags=["investigate"])
def create_investigation(
    payload: dict[str, Any],
    response: Response,
    tenant: str = Depends(require_tenant),
    sync: bool = Query(default=False, alias="sync"),
) -> dict[str, Any]:
    """对一个候选跑一次 Agent 调查。

    **两种模式**（同一段执行逻辑，见 `_execute_investigation`）：
      - **异步**（默认当 `DD_INVESTIGATE_ASYNC=1`）：落一条 pending job、入共享
        队列，**立即**返回 `202 {job_id, status}`；worker 池后台执行。前端轮询
        `GET /v1/investigations/jobs/{job_id}` 取状态/结果。这是端到端水平扩展
        的关键——HTTP worker 不再被长 LLM 调用占满。
      - **同步**（默认，或 `?sync=1`）：同请求内跑完再返回（旧行为，兼容既有
        前端与测试）。真实 LLM 时 10–60s，故前端须按"长任务"处理。

    `DD_INVESTIGATE_TIMEOUT_S` 为**整次调查**的总墙钟预算（默认 90s），两种模式
    都适用，到点失败关闭。本端点**绝不被 /v1/telemetry 调用**：热路径保持无 LLM。
    """
    candidate_id = (payload or {}).get("candidate_id")
    if not candidate_id:
        raise HTTPException(status_code=422, detail="正文需含 'candidate_id'")

    use_async = settings.investigate_async() and not sync
    if use_async:
        return _submit_async_investigation(tenant, candidate_id, response)

    outcome = _execute_investigation(tenant, candidate_id)
    if not outcome.get("ok"):
        raise HTTPException(status_code=outcome["http_status"],
                            detail=outcome["detail"])
    return outcome["response"]
