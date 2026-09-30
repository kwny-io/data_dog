"""P4-7 退化趋势预测：到达阈值的 ETA ± 区间（**不是**精确 RUL）。

定位
----
在电机真的过热前给出「预计 N 小时后到阈值 ± 区间」的前瞻预警，把运维从救火
转向预防。**诚实边界**（见 docs/p4/07_degradation_forecast.md §6）：
  - 这是**趋势外推 + 到达时间估计**，不是研究级剩余寿命（RUL）——后者要退化物
    模型 + 全生命周期数据。
  - 只对**渐变型**退化（热退化、磨损）有效；突发型故障（堵转、短路）靠即时检测。
  - 不给控制回路用（本体安全决策不依赖本预测）。

为什么 Theil–Sen 而非最小二乘
------------------------------
最小二乘对离群点极敏感：单次采样尖峰（传感器抖动/一次脉冲）会显著拉偏斜率，
把 ETA 算飞。Theil–Sen 取**成对斜率的中位数**，离群点最多影响少数几对，中位数
几乎不动——宁可保守，不给被一个野值毁掉的预测。

为什么全程"失败关闭"
--------------------
样本不足 / 斜率不显著 / 方向不一致 → `eta_hours is None`（**宁缺勿猜**）。预测错
比不预测更糟：会诱发基于错误 ETA 的运维动作。

纯函数、确定性：同输入序列 → 同输出（无随机、无时钟依赖），可复现、可测试。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median

_METHOD = "theil-sen+residual"


@dataclass(frozen=True)
class DegradationForecast:
    """一个信号对某阈值的退化预测。`eta_hours is None` = 无法预测（不猜）。"""
    signal: str
    current: float
    threshold: float
    slope_per_hour: float              # 中位斜率（带符号，值/小时）
    eta_hours: float | None            # None = 无法预测（样本不足/不显著/超视界）
    eta_lo_hours: float | None
    eta_hi_hours: float | None
    confidence: float                  # 0..1：方向一致性（趋势相对噪声的可信度）
    method: str
    n_samples: int

    def to_dict(self) -> dict[str, object]:
        return {
            "signal": self.signal, "current": self.current,
            "threshold": self.threshold, "slope_per_hour": self.slope_per_hour,
            "eta_hours": self.eta_hours, "eta_lo_hours": self.eta_lo_hours,
            "eta_hi_hours": self.eta_hi_hours, "confidence": self.confidence,
            "method": self.method, "n_samples": self.n_samples,
        }


def _pairwise_slopes(series: Sequence[tuple[float, float]]) -> list[float]:
    """所有 i<j 的成对斜率 (vj-vi)/(tj-ti)（跳过等时点的对）。"""
    out: list[float] = []
    n = len(series)
    for i in range(n):
        ti, vi = series[i]
        for j in range(i + 1, n):
            tj, vj = series[j]
            dt = tj - ti
            if dt != 0.0:
                out.append((vj - vi) / dt)
    return out


def _residual_sigma(series: Sequence[tuple[float, float]],
                    slope: float, intercept: float) -> float:
    """拟合残差的样本标准差（区间宽度来源）。样本 < 2 → 0。"""
    if len(series) < 2:
        return 0.0
    sq = sum((v - (intercept + slope * t)) ** 2 for t, v in series)
    return (sq / len(series)) ** 0.5


def forecast_to_threshold(
    series: Sequence[tuple[float, float]],
    threshold: float, *,
    signal: str = "",
    min_samples: int = 6,
    min_confidence: float = 0.5,
    horizon_hours: float | None = None,
) -> DegradationForecast:
    """对一条 `(t_秒, 值)` 序列外推到达 `threshold` 的 ETA。

    返回始终带 `slope_per_hour` 与 `confidence`（供观测）；`eta_hours` 仅在**可预测**
    （样本足、斜率显著升、置信达标、在视界内）时给数，否则 None（失败关闭）。

    参数
    ----
    series        按时间升序的 `(epoch 秒, 值)`；乱序会算错斜率，调用方须先排序。
    threshold     目标阈值（如温度上限）。
    min_samples   少于这些样本 → 不预测。
    min_confidence 方向一致性门槛（0..1）；低于它 → 不预测。
    horizon_hours 预测视界：ETA 超过它 → 不给数（外推越远越不可信）。
    """
    n = len(series)
    slope_per_hour = 0.0
    confidence = 0.0
    eta_hours: float | None = None
    eta_lo: float | None = None
    eta_hi: float | None = None
    current = float(series[-1][1]) if n else float("nan")

    slopes = _pairwise_slopes(series)
    if n >= min_samples and slopes:
        slope = median(slopes)             # 值/秒
        slope_per_hour = slope * 3600.0
        intercept = median(v - slope * t for t, v in series)
        # 方向一致性：成对斜率里正（上升）的比例。(0.5 → 0；1.0 → 1) 纯噪声≈0.5。
        frac_pos = sum(1 for s in slopes if s > 0) / len(slopes)
        confidence = max(0.0, min(1.0, (frac_pos - 0.5) * 2.0))

        rising = slope > 0.0
        significant = confidence >= min_confidence
        if rising and significant:
            if current >= threshold:
                eta_hours = 0.0
                eta_lo = eta_hi = 0.0
            else:
                eta_s = (threshold - current) / slope
                eta_h = eta_s / 3600.0
                # 视界门：超出不给数（但仍回带斜率/置信，便于观测"有趋势但太远"）
                if horizon_hours is None or eta_h <= horizon_hours:
                    sigma = _residual_sigma(series, slope, intercept)
                    # 残差（值）→ 时间不确定度：时间误差 ≈ 值误差 / 斜率（1σ）
                    sigma_t_h = (sigma / slope) / 3600.0
                    eta_hours = eta_h
                    eta_lo = max(0.0, eta_h - sigma_t_h)
                    eta_hi = eta_h + sigma_t_h

    return DegradationForecast(
        signal=signal, current=current, threshold=float(threshold),
        slope_per_hour=slope_per_hour, eta_hours=eta_hours,
        eta_lo_hours=eta_lo, eta_hi_hours=eta_hi,
        confidence=confidence, method=_METHOD, n_samples=n)


def metric_of_signal(signal: str) -> str:
    """把候选的**信号族**名映射回底层**原始指标**名。

    候选的 `signal` 是语义族（`temperature_high` / `temperature_rising` /
    `current_anomaly` / `fault_flag`，见 candidate._signal_of），而预测阈值按
    **原始指标**（`temperature` / `current`）配置。本函数做这层桥接：剥掉已知的
    语义后缀，映射回指标名；认不出时原样返回（调用方按无阈值处理，不瞎预测）。
    """
    s = (signal or "").strip()
    if s in {"temperature_high", "temperature_rising", "temperature_low"}:
        return "temperature"
    if s == "current_anomaly":
        return "current"
    return s


def series_from_rows(
    rows: Sequence[dict[str, object]], signal: str,
) -> list[tuple[float, float]]:
    """从落库遥测行抽出某信号的 `(t_秒, 值)` 序列（按事件时间升序）。

    只收「有 `occurred_at` 且该信号是有限数值」的行；解析不出的行跳过（数据质量差
    不该让整条序列报错）。这是 `GET /v1/devices/{sn}/forecast` 的数据来源。
    """
    from .candidate import _parse_time

    out: list[tuple[float, float]] = []
    for r in rows:
        dt = _parse_time(r.get("occurred_at"))
        if dt is None:
            continue
        metrics = r.get("metrics") or {}
        if not isinstance(metrics, dict) or signal not in metrics:
            continue
        try:
            v = float(metrics[signal])
        except (TypeError, ValueError):
            continue
        if v != v:                         # NaN
            continue
        out.append((dt.timestamp(), v))
    out.sort(key=lambda p: p[0])
    return out
