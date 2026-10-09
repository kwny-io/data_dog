"""`changes` 域端点：从 create_app 内联定义按域拆出。

函数体一字未改，只把 `@router.x` 换成 `@router.x`；装配在 service/app.py。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ...storage import repository as repo
from ...storage.db import session_scope
from ..deps import require_tenant

router = APIRouter()


@router.post("/v1/changes/ingest", tags=["changes"])
def ingest_change(
    payload: dict[str, Any],
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """变更系统（CMDB/发布系统）把"变了什么"推来落库（租户隔离，幂等）。

    正文（`change_id` 与 `target` 必填，其余可选；未识别的键进 `raw`，
    对 Agent 返回时展开）：

        {"change_id": "chg-1", "kind": "firmware", "target": "joint_motor.front_left_hip",
         "applied_at": "2026-09-20T10:00:00Z", "from_version": "1.2.0",
         "to_version": "1.2.1", "actor": "ops", "source": "cmdb", ...}

    与 `/v1/knowledge/*` 同源：身份只认请求头 `X-Tenant-Id`（缺失 → 401），
    正文里的同名字段一律被忽略（防越权）。

    **幂等**：同 (租户, change_id) 重复推送**覆盖更新**（不是重复插入），
    返回 `created=false`；故上游可安心重放。
    """
    body = payload or {}
    change_id = str(body.get("change_id") or "").strip()
    target = str(body.get("target") or "").strip()
    if not change_id:
        raise HTTPException(status_code=422, detail="需含非空 'change_id'")
    if not target:
        raise HTTPException(status_code=422, detail="需含非空 'target'")

    _KNOWN = ("change_id", "kind", "target", "applied_at",
              "from_version", "to_version", "actor", "source")
    record = {
        "change_id": change_id,
        "kind": str(body.get("kind") or ""),
        "target": target,
        "applied_at": str(body.get("applied_at") or ""),
        "from_version": str(body.get("from_version") or ""),
        "to_version": str(body.get("to_version") or ""),
        "actor": str(body.get("actor") or ""),
        "source": str(body.get("source") or ""),
        # 来源特有字段展开给 Agent（对齐内置表形状）
        "raw": {k: v for k, v in body.items() if k not in _KNOWN},
    }
    with session_scope() as s:
        created = repo.save_change_record(s, tenant_id=tenant, record=record)
    return {"ok": True, "change_id": change_id, "target": target,
            "created": created}
@router.get("/v1/changes", tags=["changes"])
def list_changes(
    tenant: str = Depends(require_tenant),
    target: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """列出本租户的变更记录（可按 `target` 过滤、分页）。"""
    with session_scope() as s:
        items = repo.list_change_records(
            s, tenant_id=tenant, target=target, limit=limit, offset=offset)
    return {"ok": True, "count": len(items), "items": items}
