"""`audit` 域端点：安全审计查询（A1）。

两种视野，按**出示的凭据**决定（由声明式依赖 `require_tenant_or_ops` 解析，不是按
查询参数——查询串从来不是可信身份来源）：

- `X-Ops-Token` 匹配 `DD_OPS_TOKEN` → **跨租户**（含 `-` 保留租户下的系统事件，
  例如"未带租户头的拒绝"）：运维/合规复盘用；
- 否则必须带 `X-Tenant-Id` → **只看本租户**（别人的记录既读不到、也不泄露存在性）。

注意：设了 `DD_API_KEY` 时，**最外层守卫**仍要求所有非探针端点带 `X-API-Key`
（`X-Ops-Token` / `X-Tenant-Id` 是在它之内决定视野）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from ... import audit
from ..deps import require_tenant_or_ops
from ..runtime import _DEFAULT_PAGE_SIZE, _MAX_PAGE_SIZE

router = APIRouter()


@router.get("/v1/audit", tags=["audit"])
def list_audit(
    scope: dict[str, Any] = Depends(require_tenant_or_ops),
    action: str | None = Query(default=None, description="如 auth.denied / ops.access"),
    outcome: str | None = Query(default=None, description="allowed / denied / error"),
    actor: str | None = Query(default=None, description="密钥指纹或主体标识"),
    since: float | None = Query(default=None, description="起始时间（epoch 秒）"),
    until: float | None = Query(default=None, description="结束时间（epoch 秒）"),
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """翻审计记录（新的在前）。**只读**：审计没有写入/修改端点（写入只发生在动作发生处）。"""
    if scope["scope"] == "all":
        # 读别人的审计本身就是敏感动作 → **也留痕**（"谁来审计审计者"）
        audit.record(action=audit.ACTION_AUDIT_READ_ALL,
                     outcome=audit.OUTCOME_ALLOWED,
                     actor=scope.get("key_id", ""), target="/v1/audit")
        return audit.query(tenant_id=None, action=action, outcome=outcome,
                           actor=actor, since=since, until=until,
                           limit=limit, offset=offset)

    return audit.query(tenant_id=scope["tenant_id"], action=action, outcome=outcome,
                       actor=actor, since=since, until=until,
                       limit=limit, offset=offset)
