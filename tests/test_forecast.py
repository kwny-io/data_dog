"""P4-7 退化趋势预测测试。

覆盖 docs/p4/07_degradation_forecast.md §5 的六条验收：
  1. 线性增长 → ETA 接近解析值（误差 < 10%）。
  2. 抗离群：插单个尖峰 → ETA 基本不变。
  3. 失败关闭：样本不足 / 斜率≈0 / 非单调 → eta_hours is None（不猜）。
  4. 区间：残差大 → 区间宽；残差小 → 区间窄。
  5. 视界门：ETA 超视界 → 不给数。
  6. 确定性：同输入 → 同输出。
外加端点级（设备/候选）与序列抽取的测试。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from data_dog.data_plane.forecast import (
    forecast_to_threshold,
    series_from_rows,
)

_H = 3600.0  # 一小时（秒）


def _ramp(t0: float, n: int, dt_s: float, start: float, per_hour: float):
    """线性升温序列：每小时升 per_hour，间隔 dt_s。"""
    step = per_hour * (dt_s / _H)
    return [(t0 + i * dt_s, start + i * step) for i in range(n)]


# ---------------------------------------------------------------- 1. 线性

def test_linear_eta_matches_analytic():
    """线性升温 → ETA 接近解析值。末值 = 起点 + 步数×每点增量。

    12 点、每点 +0.4167℃（5℃/h × 5min）；末值≈64.583 → (95-64.583)/5 ≈ 6.083h。
    """
    series = _ramp(0.0, 12, 300.0, start=60.0, per_hour=5.0)  # 每 5min 一个点
    fc = forecast_to_threshold(series, 95.0, signal="temperature")
    want = (95.0 - 64.58333333333333) / 5.0
    assert fc.eta_hours is not None
    assert abs(fc.eta_hours - want) / want < 0.05
    assert fc.slope_per_hour == pytest.approx(5.0, rel=0.05)
    assert fc.confidence == 1.0          # 完美单调升


# ---------------------------------------------------------------- 2. 抗离群

def test_outlier_does_not_move_eta_much():
    """单个尖峰不应毁掉预测（Theil–Sen 抗离群）。"""
    series = _ramp(0.0, 12, 300.0, start=60.0, per_hour=5.0)
    clean = forecast_to_threshold(series, 95.0)
    spiked = list(series)
    spiked[6] = (spiked[6][0], spiked[6][1] + 40.0)     # 中间插一个大尖峰
    spiky = forecast_to_threshold(spiked, 95.0)
    assert clean.eta_hours is not None and spiky.eta_hours is not None
    # 最小二乘会被这 +40 拉偏很多；Theil–Sen 应基本不动（< 25%）
    assert abs(spiky.eta_hours - clean.eta_hours) / clean.eta_hours < 0.25


# ---------------------------------------------------------------- 3. 失败关闭

def test_insufficient_samples_no_eta():
    series = _ramp(0.0, 3, 300.0, start=60.0, per_hour=5.0)
    fc = forecast_to_threshold(series, 95.0, min_samples=6)
    assert fc.eta_hours is None
    assert fc.n_samples == 3


def test_flat_slope_no_eta():
    series = [(float(i * 300), 70.0) for i in range(12)]   # 恒定 → 斜率0
    fc = forecast_to_threshold(series, 95.0)
    assert fc.eta_hours is None
    assert fc.confidence == 0.0


def test_nonmonotonic_no_eta():
    """无向噪声：一半升一半降 → 方向一致性≈0 → 不预测。"""
    vals = [70, 72, 69, 71, 68, 70, 73, 69, 71, 68, 70, 72]
    series = [(float(i * 300), float(v)) for i, v in enumerate(vals)]
    fc = forecast_to_threshold(series, 95.0, min_confidence=0.5)
    assert fc.eta_hours is None


def test_descending_no_eta():
    """下降趋势：不会"到达上阈值" → 不预测。"""
    series = _ramp(0.0, 12, 300.0, start=90.0, per_hour=-5.0)
    fc = forecast_to_threshold(series, 95.0)
    assert fc.eta_hours is None


# ---------------------------------------------------------------- 4. 区间

def test_interval_wider_with_noise():
    """残差大 → 区间宽；残差小 → 区间窄。"""
    clean = _ramp(0.0, 12, 300.0, start=60.0, per_hour=5.0)
    noisy = [(t, v + (8.0 if i % 2 else -8.0)) for i, (t, v) in enumerate(clean)]
    fc_clean = forecast_to_threshold(clean, 95.0)
    fc_noisy = forecast_to_threshold(noisy, 95.0)
    assert fc_clean.eta_hours is not None and fc_noisy.eta_hours is not None
    w_clean = fc_clean.eta_hi_hours - fc_clean.eta_lo_hours
    w_noisy = fc_noisy.eta_hi_hours - fc_noisy.eta_lo_hours
    assert w_noisy > w_clean


# ---------------------------------------------------------------- 5. 视界门

def test_horizon_gate_suppresses_far_eta():
    """很慢的升温：ETA 远超视界 → 不给数（但斜率仍回带）。"""
    series = _ramp(0.0, 12, 300.0, start=60.0, per_hour=0.5)  # (95-60)/0.5=70h
    fc = forecast_to_threshold(series, 95.0, horizon_hours=24.0)
    assert fc.eta_hours is None
    assert fc.slope_per_hour == pytest.approx(0.5, rel=0.05)
    fc2 = forecast_to_threshold(series, 95.0, horizon_hours=100.0)
    assert fc2.eta_hours is not None


# ---------------------------------------------------------------- 6. 确定性

def test_deterministic():
    series = _ramp(0.0, 12, 300.0, start=60.0, per_hour=5.0)
    a = forecast_to_threshold(series, 95.0)
    b = forecast_to_threshold(series, 95.0)
    assert a == b


def test_already_over_threshold_eta_zero():
    series = _ramp(0.0, 12, 300.0, start=96.0, per_hour=5.0)
    fc = forecast_to_threshold(series, 95.0)
    assert fc.eta_hours == 0.0


# ---------------------------------------------------------------- 序列抽取

def test_series_from_rows_filters_and_sorts():
    rows = [
        {"occurred_at": "2026-09-13T10:05:00Z", "metrics": {"temperature": 61}},
        {"occurred_at": "2026-09-13T10:00:00Z", "metrics": {"temperature": 60}},
        {"occurred_at": "bad", "metrics": {"temperature": 99}},        # 时间坏 → 跳
        {"occurred_at": "2026-09-13T10:10:00Z", "metrics": {"current": 5}},  # 无该信号 → 跳
    ]
    series = series_from_rows(rows, "temperature")
    assert len(series) == 2
    assert series[0][1] == 60 and series[1][1] == 61      # 已按时间升序


# ---------------------------------------------------------------- 端点级

def _raw(event_id, temperature, occurred_at):
    return {
        "event_id": event_id, "occurred_at": occurred_at,
        "received_at": occurred_at, "sequence": 1,
        "vendor": "unitree", "model": "go2", "firmware": "3.2.1",
        "protocol_version": "motor-log-v2",
        "component_hint": "joint_motor.front_left_hip",
        "payload_inline": json.dumps(
            {"temperature": temperature, "current": 12.0, "mode": 0}),
    }


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'fc.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    monkeypatch.setenv("DD_INVESTIGATE_OFFLINE", "1")
    monkeypatch.setenv("DD_FORECAST_THRESHOLDS", "temperature:95")
    from data_dog.storage.db import init_db, reset_engine
    reset_engine()
    init_db(url)
    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        yield c


_AUTH = {"X-Tenant-Id": "tenant-real", "X-Device-Sn": "device-go2-0001"}
_Q = {"X-Tenant-Id": "tenant-real"}


def test_device_forecast_endpoint(client):
    """灌入升温序列 → 设备预测端点给出 ETA。"""
    events = [_raw(f"e{i}", 60 + i * 2, f"2026-09-13T10:{i*5:02d}:00Z")
              for i in range(10)]   # 每 5min +2℃ → 24℃/小时
    r = client.post("/v1/telemetry", headers=_AUTH, json={"events": events})
    assert r.status_code == 200, r.text

    r = client.get("/v1/devices/device-go2-0001/forecast", headers=_Q)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    temp = next(i for i in body["items"] if i["signal"] == "temperature")
    # 末值 78℃ → (95-78)/24 ≈ 0.708h
    assert temp["eta_hours"] is not None
    assert temp["eta_hours"] == pytest.approx(0.708, abs=0.05)


def test_candidate_forecast_endpoint(client):
    """候选预测端点：取候选信号算一条。"""
    # 灌一条高温事件 → 生成候选
    ev = _raw("e1", 96, "2026-09-13T10:00:00Z")
    client.post("/v1/telemetry", headers=_AUTH, json={"events": [ev]})
    cid = client.get("/v1/candidates", headers=_Q).json()["items"][0]["candidate_id"]

    r = client.get(f"/v1/candidates/{cid}/forecast", headers=_Q)
    assert r.status_code == 200, r.text
    # 候选 signal 是语义族；metric 映射回原始指标 temperature
    assert r.json()["signal"] == "temperature_high"
    assert r.json()["metric"] == "temperature"


def test_candidate_forecast_unknown_signal(client, monkeypatch):
    """信号未配阈值 → forecast=None + 说明（不瞎预测）。"""
    monkeypatch.setenv("DD_FORECAST_THRESHOLDS", "vibration:3")
    ev = _raw("e1", 96, "2026-09-13T10:00:00Z")
    client.post("/v1/telemetry", headers=_AUTH, json={"events": [ev]})
    cid = client.get("/v1/candidates", headers=_Q).json()["items"][0]["candidate_id"]
    r = client.get(f"/v1/candidates/{cid}/forecast", headers=_Q)
    assert r.status_code == 200
    assert r.json()["forecast"] is None
