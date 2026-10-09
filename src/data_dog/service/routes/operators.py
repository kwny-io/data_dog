"""`operators` 域端点：从 create_app 内联定义按域拆出。

函数体一字未改，只把 `@router.x` 换成 `@router.x`；装配在 service/app.py。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ... import settings
from ...operator_factory import registry
from ...storage import repository as repo
from ...storage.db import session_scope
from ..deps import require_tenant

router = APIRouter()


def _operator_canary_thresholds():
    from ...operator_factory.registry import CanaryThresholds
    return CanaryThresholds(
        max_crash_rate=settings.canary_max_crash_rate(),
        max_parse_drop=settings.canary_max_parse_drop(),
        max_candidate_multiple=settings.canary_max_candidate_multiple(),
        min_samples=settings.canary_min_samples(),
    )
def _operator_signing_kwargs() -> dict[str, Any]:
    """验签凭据：配了公钥走 Ed25519，否则走 HMAC 密钥。都空 → 让注册失败关闭。"""
    pk = settings.operator_pubkey()
    if pk:
        return {"pubkey": pk}
    return {"secret": settings.operator_signing_key()}
@router.post("/v1/operators", tags=["operators"])
def register_operator(
    payload: dict[str, Any],
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """注册一个算子版本（**验签通过方可**，失败关闭）。

    正文：`{"operator_id","version","dsl_source","signature":{...}}`。
    签名绑定 (operator_id, version, sha256(dsl_source))——改一字节即验签失败。
    未配置签名密钥/公钥 → 503（不静默放行未签名的制品）。
    """
    body = payload or {}
    for k in ("operator_id", "version", "dsl_source", "signature"):
        if not body.get(k):
            raise HTTPException(status_code=422, detail=f"正文需含非空 '{k}'")
    kw = _operator_signing_kwargs()
    if not kw.get("secret") and not kw.get("pubkey"):
        raise HTTPException(
            status_code=503,
            detail="未配置签名凭据（DD_OPERATOR_SIGNING_KEY 或 DD_OPERATOR_PUBKEY）")
    with session_scope() as s:
        try:
            row = registry.register_operator(
                s, tenant_id=tenant, operator_id=body["operator_id"],
                version=body["version"], dsl_source=body["dsl_source"],
                signature=body["signature"], actor="http", **kw)
        except registry.SignatureRejected as e:
            raise HTTPException(status_code=422, detail=f"验签失败：{e}") from e
    return {"ok": True, "operator": row}
@router.get("/v1/operators", tags=["operators"])
def list_operators(
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """列出本租户的算子（按 operator_id 聚合，含当前 Active/最新版本）。"""
    with session_scope() as s:
        items = repo.list_operators(s, tenant_id=tenant)
    return {"ok": True, "count": len(items), "items": items}
@router.get("/v1/operators/{operator_id}/{version}", tags=["operators"])
def get_operator(
    operator_id: str, version: str,
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """取一个算子版本详情（含 rollout、指标快照、转移台账）。跨租户 → 404。"""
    with session_scope() as s:
        row = repo.get_operator_version(
            s, tenant_id=tenant, operator_id=operator_id, version=version)
        if row is None:
            raise HTTPException(status_code=404, detail="算子版本不存在")
        events = repo.list_rollout_events(
            s, tenant_id=tenant, operator_id=operator_id, version=version)
    return {"ok": True, "operator": row, "rollout_events": events}
@router.post("/v1/operators/{operator_id}/{version}/transition", tags=["operators"])
def transition_operator(
    operator_id: str, version: str,
    payload: dict[str, Any] | None = None,
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """状态转移（合法性由 lifecycle 状态机判定；非法 → 422）。

    正文可选：`{"to": "Shadow", "reason": "...", "evidence": {...}}`。
    """
    body = payload or {}
    target = body.get("to")
    if not target:
        raise HTTPException(status_code=422, detail="正文需含 'to'（目标状态）")
    with session_scope() as s:
        try:
            row = registry.transition_operator(
                s, tenant_id=tenant, operator_id=operator_id, version=version,
                target=target, evidence=body.get("evidence"), actor="http")
        except registry.RegistryError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    return {"ok": True, "operator": row}
@router.post("/v1/operators/{operator_id}/{version}/rollback", tags=["operators"])
def rollback_operator(
    operator_id: str, version: str,
    payload: dict[str, Any] | None = None,
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """回滚一个算子版本（→ RolledBack；非删除）。返回回滚到的上一签名版本。"""
    body = payload or {}
    reason = str(body.get("reason") or "手动回滚")
    with session_scope() as s:
        try:
            row = registry.rollback_operator(
                s, tenant_id=tenant, operator_id=operator_id, version=version,
                reason=reason, actor="http")
        except registry.RegistryError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        prev = registry.previous_signed_version(
            s, tenant_id=tenant, operator_id=operator_id, version=version)
    return {"ok": True, "operator": row, "fallback_to": prev}
@router.post("/v1/operators/{operator_id}/{version}/canary", tags=["operators"])
def start_canary(
    operator_id: str, version: str,
    payload: dict[str, Any],
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """启动/调整灰度：`{"percent": 0-100}`。分流是**确定性**的（同设备稳定落同侧）。"""
    body = payload or {}
    if "percent" not in body:
        raise HTTPException(status_code=422, detail="正文需含 'percent'")
    try:
        percent = int(body["percent"])
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail="'percent' 须为整数") from e
    with session_scope() as s:
        try:
            row = registry.start_canary(
                s, tenant_id=tenant, operator_id=operator_id, version=version,
                percent=percent, actor="http")
        except registry.RegistryError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    return {"ok": True, "operator": row}
@router.post("/v1/operators/{operator_id}/{version}/watchdog", tags=["operators"])
def operator_watchdog(
    operator_id: str, version: str,
    payload: dict[str, Any],
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """喂入 canary/stable 指标，看门狗越限则**自动回滚**。

    正文：`{"canary": {...}, "stable": {...}}`。样本不足 `DD_CANARY_MIN_SAMPLES`
    时**保守不判**（不自动推进/回滚）——见 P4-2 §6。
    """
    body = payload or {}
    with session_scope() as s:
        try:
            result = registry.evaluate_watchdog(
                s, tenant_id=tenant, operator_id=operator_id, version=version,
                canary_metrics=body.get("canary") or {},
                stable_metrics=body.get("stable") or {},
                thresholds=_operator_canary_thresholds())
        except registry.RegistryError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
    return {"ok": True, **result}
