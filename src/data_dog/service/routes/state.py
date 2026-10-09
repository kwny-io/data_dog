"""`state` 域端点：从 create_app 内联定义按域拆出。

函数体一字未改，只把 `@router.x` 换成 `@router.x`；装配在 service/app.py。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ... import settings
from ...state import get_state_store
from ...state.checkpoint import diff_checkpoint, write_checkpoint
from ...storage import repository as repo
from ...storage.db import session_scope
from ..deps import require_ops_token

router = APIRouter()


@router.post("/v1/state/checkpoint", tags=["ops"])
def state_checkpoint(
    _ops: None = Depends(require_ops_token),
) -> dict[str, Any]:
    """手动触发一次状态快照（并执行保留策略）。"""
    store = get_state_store()
    meta = write_checkpoint(store, prefix=settings.checkpoint_prefix())
    with session_scope() as s:
        repo.prune_checkpoints(s, keep=settings.checkpoint_keep())
    return {"ok": True, "checkpoint": meta}
@router.get("/v1/state/checkpoints", tags=["ops"])
def list_state_checkpoints(
    _ops: None = Depends(require_ops_token),
) -> dict[str, Any]:
    """列出快照元数据（不含 payload——不一次拉出所有状态词典）。"""
    with session_scope() as s:
        items = repo.list_checkpoints(s, limit=settings.checkpoint_keep() * 4)
    return {"ok": True, "items": items, "total": len(items)}
@router.get("/v1/state/diff", tags=["ops"])
def state_diff(
    checkpoint_id: str,
    _ops: None = Depends(require_ops_token),
) -> dict[str, Any]:
    """快照 vs 当前状态差异（新增/变更/删除的 key）——验证恢复正确性。"""
    return {"ok": True, "diff": diff_checkpoint(get_state_store(), checkpoint_id)}
