"""`forecast` 域端点：从 create_app 内联定义按域拆出。

函数体一字未改，只把 `@router.x` 换成 `@router.x`；装配在 service/app.py。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ... import settings
from ...core import metrics
from ...storage import repository as repo
from ...storage.db import session_scope
from ..deps import require_tenant

router = APIRouter()


@router.get("/v1/devices/{device_sn}/forecast", tags=["forecast"])
def get_device_forecast(
    device_sn: str,
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """该设备各已配阈值信号的退化预测（到达阈值 ETA ± 区间）。

    **按需现算**：从落库遥测重建各信号序列 → Theil–Sen 外推。不给 ETA 的信号
    也回带斜率/置信（`eta_hours is None` = 数据不足/不显著/超视界，失败关闭）。
    """
    from ...data_plane.forecast import forecast_to_threshold, series_from_rows
    thresholds = settings.forecast_thresholds()
    with session_scope() as s:
        rows = repo.list_telemetry(s, tenant_id=tenant, device_sn=device_sn)

    items = []
    for signal, threshold in thresholds.items():
        series = series_from_rows(rows, signal)
        fc = forecast_to_threshold(
            series, threshold, signal=signal,
            min_samples=settings.forecast_min_samples(),
            min_confidence=settings.forecast_min_conf(),
            horizon_hours=settings.forecast_horizon_h())
        metrics.observe_forecast(fc.eta_hours is not None, fc.confidence)
        items.append(fc.to_dict())
    return {"ok": True, "device_sn": device_sn,
            "thresholds": thresholds, "items": items}
@router.get("/v1/candidates/{candidate_id}/forecast", tags=["forecast"])
def get_candidate_forecast(
    candidate_id: str,
    tenant: str = Depends(require_tenant),
) -> dict[str, Any]:
    """该候选所在设备、其信号（候选 signal）的退化预测。

    信号来自候选本身；设备来自候选行——故只对「候选关心的那个信号」算一条。
    候选取自落库遥测（与调查同源），不另存预测。
    """
    from ...data_plane.forecast import (
        forecast_to_threshold,
        metric_of_signal,
        series_from_rows,
    )
    with session_scope() as s:
        cand = repo.get_candidate(s, candidate_id, tenant_id=tenant)
        if cand is None:
            raise HTTPException(status_code=404, detail="候选不存在")
        rows = repo.list_telemetry(s, tenant_id=tenant,
                                   device_sn=cand["device_sn"])

    signal = cand["signal"]
    # 候选 signal 是语义族（temperature_high），阈值按原始指标（temperature）配
    metric = metric_of_signal(signal)
    threshold = settings.forecast_thresholds().get(metric)
    if threshold is None:
        return {"ok": True, "candidate_id": candidate_id, "device_sn":
                cand["device_sn"], "signal": signal, "metric": metric,
                "forecast": None,
                "reason": "该信号未配阈值（DD_FORECAST_THRESHOLDS），不预测"}
    series = series_from_rows(rows, metric)
    fc = forecast_to_threshold(
        series, threshold, signal=signal,
        min_samples=settings.forecast_min_samples(),
        min_confidence=settings.forecast_min_conf(),
        horizon_hours=settings.forecast_horizon_h())
    metrics.observe_forecast(fc.eta_hours is not None, fc.confidence)
    return {"ok": True, "candidate_id": candidate_id,
            "device_sn": cand["device_sn"], "signal": signal,
            "metric": metric, "forecast": fc.to_dict()}
