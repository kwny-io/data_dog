"""`tenant` 域端点：租户级数据导出与删除（⑥ 合规）。

两个端点，两档鉴权（破坏性动作的门槛必须更高）
--------------------------------------------
- `GET /v1/tenant/export`：**租户自己**导出（声明式依赖 `require_tenant`，租户从
  `X-Tenant-Id` 来）。导出是读，但读的是**自己的全部数据**，所以仍要留痕。
- `POST /v1/tenant/purge`：**只有运维面**能调（`require_ops_token_scope`），且必须在
  `X-Confirm-Tenant` 里**原样复述**要删的租户 id。删除不可逆，"手滑删错租户"没有
  回滚——把"确认"做成必须显式提供的一个 header，比在文档里写一句"请谨慎操作"有用。

为什么运维不能借这个端点导出别的租户
----------------------------------
导出只给了租户视野：运维要导出某租户的数据，需要**该租户的密钥**（或者临时把自己
的运维令牌换成租户身份）。这是有意的——"能删"与"能读走"是两种权力，把它们绑在同一个
令牌上，等于给运维令牌加了"可批量外带任意租户数据"的隐含能力。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from ... import settings
from ...errors import ToolErrorCode
from ...tenant_data import export_tenant, purge_tenant
from ..deps import require_ops_token_scope, require_tenant

router = APIRouter()


class PurgeRequest(BaseModel):
    tenant_id: str = Field(min_length=1)
    dry_run: bool = False
    batch_size: int = Field(default=500, ge=1)
    max_batches: int = Field(default=200, ge=1)


def _bad_request(detail: str, reason: str) -> HTTPException:
    return HTTPException(status_code=400, detail={
        "ok": False, "error": ToolErrorCode.INVALID_INPUT.value,
        "detail": detail, "reason": reason})


@router.get("/v1/tenant/export", tags=["tenant"])
def export_my_tenant(
    tenant_id: str = Depends(require_tenant),
    limit: int | None = None,
) -> dict[str, Any]:
    """导出本租户数据快照。**每表上限**由 `DD_TENANT_EXPORT_LIMIT` 控制。

    `truncated=true` 表示有表被上限截断——调用方据此判断快照是否完整（有界快照不等于
    全量导出，这一点在响应里明说，不靠文档）。
    """
    per_table = limit if limit is not None else settings.tenant_export_limit()
    if per_table < 1:
        raise _bad_request("limit 必须 ≥ 1", "invalid_limit")
    return export_tenant(tenant_id, per_table_limit=per_table)


@router.post("/v1/tenant/purge", tags=["tenant"])
def purge_tenant_data(
    body: PurgeRequest,
    scope: dict[str, Any] = Depends(require_ops_token_scope),
    x_confirm_tenant: str | None = Header(default=None, alias="X-Confirm-Tenant"),
) -> dict[str, Any]:
    """删除某租户的全部数据（**破坏性、不可逆**，仅运维面）。

    必须在 `X-Confirm-Tenant` 里原样复述 `tenant_id`；不一致 → 400 且**不删任何东西**。
    """
    if (x_confirm_tenant or "").strip() != body.tenant_id:
        raise _bad_request(
            "删除不可逆：请在 X-Confirm-Tenant 里原样复述要删除的 tenant_id",
            "confirm_required")
    try:
        return purge_tenant(body.tenant_id, batch_size=body.batch_size,
                            max_batches=body.max_batches, dry_run=body.dry_run)
    except ValueError as e:
        # 保留租户 `-` 不可删、空 tenant_id 等 → 400（是调用方的问题，不是服务故障）
        raise _bad_request(str(e), "invalid_tenant") from e
