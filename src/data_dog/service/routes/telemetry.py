"""`telemetry` 域端点：从 create_app 内联定义按域拆出。

函数体一字未改，只把 `@router.x` 换成 `@router.x`；装配在 service/app.py。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response

from ... import settings
from ...core import metrics
from ...core.redact import redact
from ...storage import repository as repo
from ...storage.db import session_scope
from ..deps import require_tenant_and_device
from ..runtime import _log, _state, _stats_dict, get_ingest_plane
from ..write_behind import get_writer

router = APIRouter()


@router.post("/v1/telemetry", tags=["ingest"])
def ingest_telemetry(
    payload: dict[str, Any],
    response: Response,
    auth: dict[str, Any] = Depends(require_tenant_and_device),
) -> dict[str, Any]:
    """接收一批原始日志，跑数据面，产出候选并落库。

    正文格式：
        {"events": [ {原始日志}, ... ]}
    身份来自请求头（X-Tenant-Id / X-Device-Sn），正文里若有同名字段一律被忽略。
    """
    events = payload.get("events")
    if not isinstance(events, list) or not events:
        raise HTTPException(status_code=422, detail="正文需含非空 'events' 数组")
    if len(events) > 1000:
        raise HTTPException(status_code=422, detail="单批 events 上限 1000 条")

    # **长驻数据面**（带锁），不是每请求新建。理由见 `runtime.get_ingest_plane`
    # 的 docstring：窗口必须跨请求累积，否则 5 秒门槛的持续性规则恒假。
    got = get_ingest_plane()
    if got is None:
        _state.rejected_requests += 1
        raise HTTPException(status_code=503,
                            detail=f"算子绑定不可用：{_state.build_error}")
    plane, lock = got

    _state.request_count += 1
    _log.info("接入请求", extra={
        "tenant_id": auth["tenant_id"], "device_sn": auth["device_sn"],
        "events": len(events),
    })
    with lock:
        result = plane.process_batch(events, auth)
    metrics.observe_data_plane(result.stats)
    metrics.candidates_created_total.inc(len(result.candidates))
    _log.info("接入完成", extra={
        "ingested": result.stats.ingested, "resolved": result.stats.resolved,
        "rejected": result.stats.rejected, "candidates": len(result.candidates),
    })

    candidates_out = [c.to_dict() for c in result.candidates]
    # 批内有事件被拒（限流/校验不过/绑定不到）时，`ok` 仍为 True（请求确实被正常处理了），
    # 所以顶上加一个机器可读的 `partial`：调用方不该把 200 + ok 当成“全部落了”。
    # ⚠️ `stats.ingested` 是**收到**的条数（含被拒），真正的落库数看 `stats.normalized`
    # 或 `persisted.*`；详见 docs/data_plane.md §3.5（#102）。
    _stats = _stats_dict(result)
    partial = int(_stats.get("rejected", 0)) > 0

    # write-behind（默认关）：落库入队即返回；队列满/线程未起 → 503 失败关闭。
    # ⚠️ 语义代价（HTTP 200 不再代表已落库）见 docs/write_path_batching.md §10。
    if settings.write_behind():
        writer = get_writer()
        if writer is None:
            response.status_code = 503
            return {"ok": False, "error": "write_behind_unavailable",
                    "detail": "DD_WRITE_BEHIND=1 但后台写入线程未运行",
                    "stats": _stats, "candidates": candidates_out,
                    "partial": partial}
        if not writer.submit(tenant_id=auth["tenant_id"],
                             normalized=result.normalized,
                             candidates=result.candidates):
            response.status_code = 503
            return {"ok": False, "error": "write_behind_queue_full",
                    "detail": "写队列已满（落库跟不上）；失败关闭，请重试",
                    "stats": _stats, "candidates": candidates_out,
                    "partial": partial}
        return {"ok": True, "stats": _stats,
                "persisted": {"mode": "write_behind", "queued": True},
                "candidates": candidates_out, "partial": partial}

    # 落库（幂等 upsert）：规范化遥测 + 候选（同步路径——默认行为）
    persisted = {"telemetry": 0, "candidates": 0}
    try:
        with session_scope() as s:
            persisted["telemetry"] = repo.save_telemetry(
                s, result.normalized, tenant_id=auth["tenant_id"])
            persisted["candidates"] = repo.save_candidates(
                s, result.candidates, tenant_id=auth["tenant_id"])
    except Exception as e:                        # noqa: BLE001
        # 落库失败不吞掉已算出的结果，但要在响应里标明（不静默降级）
        response.status_code = 207
        return {
            "ok": False, "error": "persist_failed",
            "detail": redact(f"{type(e).__name__}: {e}"),
            "stats": _stats,
            "candidates": [c.to_dict() for c in result.candidates],
            "partial": partial,
        }

    return {
        "ok": True,
        "stats": _stats,
        "persisted": persisted,
        "candidates": [c.to_dict() for c in result.candidates],
        "partial": partial,
    }
