"""`feedback` 域端点：从 create_app 内联定义按域拆出。

函数体一字未改，只把 `@router.x` 换成 `@router.x`；装配在 service/app.py。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...alerting.lifecycle import TRANSITIONS
from ...eval_feedback import summarize_feedback
from ...storage import repository as repo
from ...storage.db import session_scope
from ..deps import require_tenant

router = APIRouter()


_FEEDBACK_KINDS = {"false_positive", "repaired", "no_action"}
_FEEDBACK_TARGET = {"false_positive": "rejected", "repaired": "closed"}
@router.post("/v1/feedback", tags=["alerting"])
def submit_feedback(
    payload: dict[str, Any],
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """提交人工反馈（误报 / 已修 / 无需处理），并**回灌**到告警状态机。

    回灌规则（确定性；状态转移不合法则**如实拒绝**，不强行改）：
      - `false_positive` → 告警转 `rejected`（rejected 是同 Episode 的活跃态，复发可
        重回 detected）
      - `repaired`       → 告警转 `closed`（**仅当当前状态可一步到 closed**，即
        confirmed/escalated；detected/recovering 无直达边，则只存档不改状态）
      - `no_action`      → 仅存档，不改状态
    反馈始终以记录形式落库（可事后审计），状态变更追加进告警 history。
    """
    body = payload or {}
    event_id = body.get("event_id")
    kind = body.get("kind")
    note = body.get("note", "")
    if not event_id or not kind:
        raise HTTPException(status_code=422, detail="正文需含 'event_id' 与 'kind'")
    if kind not in _FEEDBACK_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"未知反馈类型 {kind!r}（可用：{sorted(_FEEDBACK_KINDS)}）")

    with session_scope() as s:
        alert = repo.get_alert(s, event_id, tenant_id=tenant)
        if alert is None:
            raise HTTPException(status_code=404, detail="告警不存在")
        repo.save_feedback(s, tenant_id=tenant, event_id=event_id,
                           kind=kind, note=note)
        target = _FEEDBACK_TARGET.get(kind)
        applied: str | None = None
        if target is not None:
            legal = target in TRANSITIONS.get(alert["state"], set())
            if legal:
                repo.set_alert_state(s, tenant_id=tenant, event_id=event_id,
                                     to_state=target,
                                     reason=f"人工反馈：{kind}")
                applied = target
        updated = repo.get_alert(s, event_id, tenant_id=tenant)

    return {
        "ok": True,
        "event_id": event_id,
        "kind": kind,
        "state_applied": applied,          # null = 未改状态（no_action 或转移不合法）
        "state_now": updated["state"] if updated else None,
        "alert": updated,
    }
@router.get("/v1/alerts/{event_id}/feedback", tags=["alerting"])
def list_alert_feedback(
    event_id: str,
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """读某条告警的人工反馈历史（租户内；跨租户 → 404）。"""
    with session_scope() as s:
        alert = repo.get_alert(s, event_id, tenant_id=tenant)
        if alert is None:
            raise HTTPException(status_code=404, detail="告警不存在")
        items = repo.list_feedback_for_event(s, tenant_id=tenant,
                                             event_id=event_id)
    return {"ok": True, "event_id": event_id, "count": len(items), "items": items}
@router.get("/v1/eval/feedback", tags=["eval"])
def eval_feedback(
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """把人工反馈当**评估标签**，度量 Agent 判得准不准（租户作用域）。

    此前人工反馈只落库 + 改状态——那是"存档"，不是"回灌"。这里把每条反馈与
    它告警对应的 Agent 裁决（`reports.disposition`）关联，算出一致率 / 误报率 /
    漏报率。分母只含**有标签**的行（人工明确判为误报/已修的）；`no_action`
    语义模糊，计入 unlabeled、不混进分子分母（宁可少算，不硬凑数字）。

    与 `/v1/eval/*` 的离线评测集（evals/）互补：那是**离线回归**，这是**线上
    真实反馈**驱动的度量。两者共同回答"Agent 到底判得准不准"。
    """
    with session_scope() as s:
        rows = repo.list_feedback_with_report(s, tenant_id=tenant)
    return {"ok": True, "summary": summarize_feedback(rows), "rows": rows}
