"""Prometheus 指标（SRE 可观测性）。

设计取舍
--------
- **用 `prometheus_client` 的标准出口格式**：`/metrics` 返回 Prometheus 文本格式，
  可被 Prometheus 直接抓取，无需自建解析。此前返回自定义 JSON，抓取方无法消费。
- **不把"业务总量"做成 O(n) 全表扫描**：此前 `/metrics` 用 `len(list_candidates(s))`
  之类把**全表拉进内存**再取长度，数据一多就会拖垮服务。改用 `COUNT(*)` 聚合查询。
- **标签基数受控**：`path` 用**路由模板**（如 `/v1/candidates`）而非实际 URL，
  避免 `/v1/x?id=...` 之类把标签基数撑爆（Prometheus 的大忌）。
- **零业务耦合**：本模块只提供注册表与采集原语，业务侧按需 import。
"""
from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# 用独立注册表，避免与进程内其它库（如 gunicorn）的默认注册表冲突，
# 也让测试可以拿到干净实例。
registry = CollectorRegistry()

# ---------------------------------------------------------------- HTTP

http_requests_total = Counter(
    "data_dog_http_requests_total",
    "HTTP 请求总数",
    labelnames=("method", "path", "status"),
    registry=registry,
)

http_request_duration_seconds = Histogram(
    "data_dog_http_request_duration_seconds",
    "HTTP 请求耗时（秒）",
    labelnames=("method", "path"),
    # 覆盖 5ms ~ 2.5s，适配本服务"单请求几十毫秒"的量级
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
    registry=registry,
)

# ---------------------------------------------------------------- 业务

# 接入结果分类（与 DataPlaneStats 对齐）
telemetry_events_total = Counter(
    "data_dog_telemetry_events_total",
    "处理的遥测事件总数（按结果分类）",
    labelnames=("result",),      # ingested / resolved / rejected
    registry=registry,
)

telemetry_rejected_by_reason = Counter(
    "data_dog_telemetry_rejected_by_reason_total",
    "被失败关闭的遥测事件（按细分原因）",
    labelnames=("reason",),      # gateway / resolve / rate_limited / payload_ref_denied ...
    registry=registry,
)

candidates_created_total = Counter(
    "data_dog_candidates_created_total",
    "产出的候选异常总数",
    registry=registry,
)

# 调查（Agent）执行结果：result ∈ {ok, not_found, timeout, failed}
investigations_total = Counter(
    "data_dog_investigations_total",
    "调查任务总数（按结果分类）",
    labelnames=("result",),
    registry=registry,
)

# 库内累计量（用 COUNT(*) 聚合，不做全表加载）
db_rows = Gauge(
    "data_dog_db_rows",
    "各表当前行数",
    labelnames=("table",),       # candidates / alerts / outbox_pending
    registry=registry,
)

# ---------------------------------------------------------------- 限流

rate_limited_total = Counter(
    "data_dog_rate_limited_total",
    "被限流拒绝的事件总数",
    registry=registry,
)

# ---------------------------------------------------------------- 进程信息（SRE 关注）

info = Gauge(
    "data_dog_info",
    "服务信息（值为 1，具体信息在标签里）",
    labelnames=("version",),
    registry=registry,
)

auth_enabled = Gauge(
    "data_dog_auth_enabled",
    "是否启用 API Key 鉴权（1=启用，0=未启用）。未启用即对外裸奔，需告警",
    registry=registry,
)

resolver_ready = Gauge(
    "data_dog_resolver_ready",
    "算子绑定是否可用（1=可用，0=不可用）",
    registry=registry,
)

uptime_seconds = Gauge(
    "data_dog_uptime_seconds",
    "服务运行时长（秒）",
    registry=registry,
)


def observe_http(method: str, path: str, status: int, duration_s: float) -> None:
    """记录一次 HTTP 请求（path 必须是**路由模板**，不是实际 URL）。"""
    http_requests_total.labels(method=method, path=path, status=str(status)).inc()
    http_request_duration_seconds.labels(method=method, path=path).observe(duration_s)


def observe_data_plane(stats) -> None:
    """把一次数据面处理的统计量记进指标。"""
    telemetry_events_total.labels(result="ingested").inc(stats.ingested)
    telemetry_events_total.labels(result="resolved").inc(stats.resolved)
    telemetry_events_total.labels(result="rejected").inc(stats.rejected)
    for reason, n in (stats.rejected_by_reason or {}).items():
        telemetry_rejected_by_reason.labels(reason=reason).inc(n)
        if reason == "rate_limited":
            rate_limited_total.inc(n)


def render() -> bytes:
    """渲染为 Prometheus 文本格式（供 `/metrics` 直接返回）。"""
    return generate_latest(registry)
