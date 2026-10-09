"""`queries` 域端点：从 create_app 内联定义按域拆出。

函数体一字未改，只把 `@router.x` 换成 `@router.x`；装配在 service/app.py。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ... import settings
from ...evidence.refs import EvidenceResolver, make_candidate_metricwin_source
from ...storage import models as M
from ...storage import repository as repo
from ...storage.db import session_scope
from ..deps import require_tenant
from ..evidence import RepoEvidenceBridge
from ..runtime import _DEFAULT_PAGE_SIZE, _MAX_PAGE_SIZE, _STARTED_AT, _state

router = APIRouter()


@router.get("/v1/summary", tags=["query"])
def summary(
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """仪表盘聚合只读端点（租户作用域）。

    一次给前端首屏所需的总量数字，省掉"拉三个列表再数长度"的往返。所有计数
    都**限定在当前租户**（用 COUNT/GROUP BY 聚合，不拉全表）；`uptime_s` /
    `resolver_ready` / `auth_enabled` 是进程级事实，与租户无关。
    """
    with session_scope() as s:
        candidates_total = repo.count_rows(s, M.CandidateRow, tenant_id=tenant)
        alerts_by_state = repo.count_alerts_by_state(s, tenant_id=tenant)
        outbox_pending = repo.count_rows(
            s, M.AlertOutboxRow, tenant_id=tenant, status="pending")
    return {
        "ok": True,
        "uptime_s": round(time.time() - _STARTED_AT, 1),
        "candidates_total": candidates_total,
        "alerts_by_state": alerts_by_state,
        "outbox_pending": outbox_pending,
        "resolver_ready": _state.resolver is not None,
        "auth_enabled": bool(settings.api_key()),
    }
@router.get("/v1/candidates", tags=["query"])
def list_candidates(
    tenant: str = Depends(require_tenant),
    device_sn: str | None = Query(default=None),
    component_ref: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    with session_scope() as s:
        rows = repo.list_candidates(s, tenant_id=tenant, device_sn=device_sn,
                                    component_ref=component_ref,
                                    limit=limit, offset=offset)
        total = repo.count_rows(s, M.CandidateRow, tenant_id=tenant)
    return {"ok": True, "count": len(rows), "total": total,
            "limit": limit, "offset": offset,
            "has_more": offset + len(rows) < total, "items": rows}
@router.get("/v1/alerts", tags=["query"])
def list_alerts(
    tenant: str = Depends(require_tenant),
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    with session_scope() as s:
        rows = repo.list_alerts(s, tenant_id=tenant, limit=limit, offset=offset)
        total = repo.count_rows(s, M.AlertRow, tenant_id=tenant)
    return {"ok": True, "count": len(rows), "total": total,
            "limit": limit, "offset": offset,
            "has_more": offset + len(rows) < total, "items": rows}
@router.get("/v1/outbox", tags=["query"])
def list_outbox(
    tenant: str = Depends(require_tenant),
    status: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    with session_scope() as s:
        rows = repo.list_outbox(s, tenant_id=tenant, status=status,
                                limit=limit, offset=offset)
        total = repo.count_rows(s, M.AlertOutboxRow, tenant_id=tenant)
    return {"ok": True, "count": len(rows), "total": total,
            "limit": limit, "offset": offset,
            "has_more": offset + len(rows) < total, "items": rows}
@router.get("/v1/candidates/{candidate_id}", tags=["query"])
def get_candidate(
    candidate_id: str,
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """取单个候选详情。跨租户（或不存在）→ 404，不泄露存在性。"""
    with session_scope() as s:
        item = repo.get_candidate(s, candidate_id, tenant_id=tenant)
    if item is None:
        raise HTTPException(status_code=404, detail="候选不存在")
    return {"ok": True, "item": item}
@router.get("/v1/candidates/{candidate_id}/evidence", tags=["query"])
def get_candidate_evidence(
    candidate_id: str,
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Agent 所见证据：候选窗口的遥测 + 参照 + 候选自带证据 + 厂商知识。

    证据由 `RepoEvidenceBridge` 从**同一批落库遥测**重建（与调查时同源），
    因此前端"先看证据、再点调查"不会看到两套对不上的数字。
    """
    with session_scope() as s:
        cand = repo.get_candidate(s, candidate_id, tenant_id=tenant)
        if cand is None:
            raise HTTPException(status_code=404, detail="候选不存在")
        rows = repo.list_telemetry(s, tenant_id=tenant,
                                   device_sn=cand["device_sn"])

    bridge = RepoEvidenceBridge(rows, device_sn=cand["device_sn"])
    tel = bridge.query_telemetry(
        cand["device_sn"], cand["component_ref"],
        cand["window_start"], cand["window_end"])
    tel = tel if isinstance(tel, dict) and tel.get("ok") is not False else {}
    ctype = (cand["component_ref"].split(".", 1) or [""])[0]
    vendor = bridge.query_vendor_knowledge(ctype)

    return {
        "ok": True,
        "candidate_id": candidate_id,
        "device_sn": cand["device_sn"],
        "component_ref": cand["component_ref"],
        "window": {"start": cand["window_start"], "end": cand["window_end"]},
        "metrics": tel.get("metrics", {}),
        "reference_window_metrics": tel.get("reference_window_metrics") or {},
        "evidence": cand.get("evidence", []),
        "vendor_knowledge": vendor,
    }
@router.get("/v1/candidates/{candidate_id}/refs", tags=["query"])
def resolve_candidate_refs(
    candidate_id: str,
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """把候选的 evidence_refs（不透明引用）解析成**可读证据**。

    引用形如 `metricwin://<candidate_id>` / `rawlog://<event_id>` 等；此前只是
    不透明字符串，审计方读不回内容。这里用 `EvidenceResolver` 解析，并把
    `metricwin` 源**换成按落库候选反查的真实源**（租户作用域，跨租户查不到 →
    not_found，不泄露存在性）。解析逻辑（语法/权限/完整性/有效期）复用同一实现。
    """
    with session_scope() as s:
        cand = repo.get_candidate(s, candidate_id, tenant_id=tenant)
        if cand is None:
            raise HTTPException(status_code=404, detail="候选不存在")

        def _lookup(cid: str) -> dict[str, Any] | None:
            return repo.get_candidate(s, cid, tenant_id=tenant)

        resolver = EvidenceResolver()
        resolver.replace_source(
            "metricwin", make_candidate_metricwin_source(_lookup))
        refs = list(cand.get("evidence_refs", [])) or [
            f"metricwin://{candidate_id}"]
        out = resolver.resolve_all(refs)

    out["candidate_id"] = candidate_id
    return out
@router.get("/v1/reports", tags=["query"])
def list_reports(
    tenant: str = Depends(require_tenant),
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """分页列出本租户的历史调查报告（调查结果可复用，不必重跑 LLM）。"""
    with session_scope() as s:
        rows = repo.list_reports(s, tenant_id=tenant, limit=limit, offset=offset)
        total = repo.count_rows(s, M.ReportRow, tenant_id=tenant)
    return {"ok": True, "count": len(rows), "total": total,
            "limit": limit, "offset": offset,
            "has_more": offset + len(rows) < total, "items": rows}
