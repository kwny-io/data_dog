"""`notify` 域端点：从 create_app 内联定义按域拆出。

函数体一字未改，只把 `@router.x` 换成 `@router.x`；装配在 service/app.py。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...storage import repository as repo
from ...storage.db import session_scope
from ..deps import require_tenant

router = APIRouter()


@router.post("/v1/alerts/{event_id}/ack", tags=["notify"])
def ack_alert(
    event_id: str,
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """确认告警（停止升级）：该告警所有 pending 投递置 acked。

    幂等：重复 ack 不会二次改动（第二次改动条数为 0）。被确认意味着有人接手，
    若告警状态允许则一并推进到 `recovering`（转移不合法则只 ack，不硬造状态）。
    """
    from ...alerting.escalation import EscalationEngine
    with session_scope() as s:
        if repo.get_alert(s, event_id, tenant_id=tenant) is None:
            raise HTTPException(status_code=404, detail="告警不存在")
    engine = EscalationEngine()
    acked = engine.ack(tenant_id=tenant, event_id=event_id)
    return {"ok": True, "event_id": event_id, "acked": acked}
@router.get("/v1/oncall", tags=["notify"])
def get_oncall(
    tenant: str = Depends(require_tenant),
    domain: str | None = None,
    at: float | None = None,
) -> dict[str, Any]:
    """当前值班表。`domain` 可选过滤；`at`（epoch 秒）可选指定时刻。

    另给 `resolved`：在 `at`（默认现在）解析出的"当前一线值班人"（无则回退更
    高级别）——这就是告警实际会派给谁。
    """
    from ...alerting.oncall import current_oncall, rows_to_entries
    now = float(at) if at is not None else time.time()
    with session_scope() as s:
        rows = repo.list_rota(s, tenant_id=tenant, domain=domain)
    entries = rows_to_entries(rows)
    resolved = current_oncall(entries, domain=domain, at=now)
    return {"ok": True, "count": len(rows),
            "items": [e.to_dict() for e in entries],
            "resolved": resolved.to_dict() if resolved else None}
@router.put("/v1/oncall", tags=["notify"])
def put_oncall(
    payload: dict[str, Any],
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """设置一条值班（`{domain, level, assignee, start_at, end_at, channel, target}`）。

    `rota_id` 由 (domain, level, start_at) 确定性派生——同一排班反复 PUT 收敛到
    同一行。`start_at`/`end_at` 为 epoch 秒（`end_at` 须晚于 `start_at`）。
    """
    from ...alerting.oncall import make_rota_id
    body = payload or {}
    try:
        domain = str(body.get("domain") or "*")
        level = int(body.get("level", 1))
        assignee = str(body.get("assignee") or "")
        start_at = float(body["start_at"])
        end_at = float(body["end_at"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail="需含 {domain?, level, assignee, start_at, end_at}（时间为 epoch 秒）"
        ) from None
    if level < 1:
        raise HTTPException(status_code=422, detail="level 须 ≥ 1")
    if end_at <= start_at:
        raise HTTPException(status_code=422, detail="end_at 须晚于 start_at")
    channel = str(body.get("channel") or "webhook")
    target = str(body.get("target") or "")
    rota_id = make_rota_id(domain, level, start_at)
    with session_scope() as s:
        row = repo.upsert_rota(
            s, tenant_id=tenant, rota_id=rota_id, domain=domain,
            level=level, assignee=assignee, start_at=start_at,
            end_at=end_at, channel=channel, target=target)
    return {"ok": True, "rota": row}
@router.get("/v1/notifications", tags=["notify"])
def list_notifications(
    tenant: str = Depends(require_tenant),
    event_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """投递记录（审计）：每次通知尝试一行，含状态/值班人/ack 截止。

    可按 `event_id` / `status` 过滤。这是回答"这条告警到底通知到谁、确认了没"
    的落点。
    """
    with session_scope() as s:
        rows = repo.list_deliveries(s, tenant_id=tenant,
                                    event_id=event_id, status=status,
                                    limit=limit)
    return {"ok": True, "count": len(rows), "items": rows}
