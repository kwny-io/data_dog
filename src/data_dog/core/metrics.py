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

# ---------------------------------------------------------------- 异步调查（G1）

# 异步调查 job 的状态流转计数（每次进入该状态 +1）
# status ∈ {pending, running, succeeded, failed, cancelled}
investigation_jobs_total = Counter(
    "data_dog_investigation_jobs_total",
    "异步调查任务数（按状态）",
    labelnames=("status",),
    registry=registry,
)

# 待办队列深度（pending 且尚未被 worker 取走的 job 数；>0 持续升高说明消费跟不上）
investigation_queue_depth = Gauge(
    "data_dog_investigation_queue_depth",
    "异步调查待办队列深度",
    registry=registry,
)

# 单次异步调查的执行耗时（从 worker 领取到落终态）
investigation_job_duration_seconds = Histogram(
    "data_dog_investigation_job_duration_seconds",
    "异步调查执行耗时（秒）",
    # 覆盖离线毫秒级到真实 LLM 的分钟级
    buckets=(0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 120.0),
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

# ---------------------------------------------------------------- 流式接入

stream_messages_total = Counter(
    "data_dog_stream_messages_total",
    "流式源收到的消息总数（按来源）",
    labelnames=("source",),      # file / http / mqtt / kafka
    registry=registry,
)

stream_candidates_total = Counter(
    "data_dog_stream_candidates_total",
    "流式接入产出的候选总数",
    registry=registry,
)

# 推式源队列满时丢弃的消息（背压证据：>0 说明消费跟不上，需扩副本/调队列）
stream_dropped_total = Counter(
    "data_dog_stream_dropped_total",
    "流式源因队列满而丢弃的消息总数",
    labelnames=("source",),
    registry=registry,
)

stream_up = Gauge(
    "data_dog_stream_up",
    "流式消费线程是否存活（1=存活，0=未启用或已退出）",
    registry=registry,
)

# ---------------------------------------------------------------- 事件时间 / 迟到（G2）

# 迟到过多（早于水位线）被拒的事件数——静默丢弃是最难排查的错误，故必须可见。
events_late_total = Counter(
    "data_dog_events_late_total",
    "迟到过多（早于水位线）被拒的事件总数",
    labelnames=("source",),
    registry=registry,
)

# 容忍窗内被并入已有组的迟到事件数（"有界的迟到容忍"命中次数）。
late_merged_total = Counter(
    "data_dog_late_merged_total",
    "容忍窗内并入已有组的迟到事件总数",
    labelnames=("source",),
    registry=registry,
)

# 消费延迟：消息进入消费循环 → 落库完成（含解析+检测+落库的整段端到端；P4-8）
stream_consume_latency_seconds = Histogram(
    "data_dog_stream_consume_latency_seconds",
    "流式消息消费延迟（收到→落库完成，秒）",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
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
