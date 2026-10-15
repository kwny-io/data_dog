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

# 延迟直方图的桶是**契约**：谁把上限改小，长尾就重新不可观测。提成具名常量，
# 让 `tests/test_metrics_buckets.py` 能直接断言覆盖范围（而不是只靠注释）。
HTTP_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5,
                        5.0, 10.0, 30.0, 60.0, 120.0)
JOB_DURATION_BUCKETS = (0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 120.0)

http_request_duration_seconds = Histogram(
    "data_dog_http_request_duration_seconds",
    "HTTP 请求耗时（秒）",
    labelnames=("method", "path"),
    # 覆盖 5ms ~ 120s。**长尾是必需的**：绝大多数端点几十毫秒，但 POST /v1/investigations
    # 的同步路径要等一次真 LLM（10–60s）；顶格只到 2.5s 时它们全落进 +Inf，
    # **那条路的 P95/P99 实际不可观测**（异步路径另有 investigation_job_duration_seconds）。
    buckets=HTTP_LATENCY_BUCKETS,
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

# write-behind（DD_WRITE_BEHIND=1 才用）：result ∈ {enqueued, written, failed, rejected_full}。
# failed / rejected_full **必须告警**——开了 write-behind 后，这两种情况都意味着
# "响应已 200 但数据没落库"，是静默丢数据的唯一可见窗口。
write_behind_total = Counter(
    "data_dog_write_behind_total",
    "write-behind 写路径计数（按结果分类）",
    labelnames=("result",),
    registry=registry,
)

write_behind_queue_depth = Gauge(
    "data_dog_write_behind_queue_depth",
    "write-behind 队列当前深度（持续 >0 说明落库跟不上）",
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

# 分区有效期（A9）：**上报时间戳而不是"还剩几天"**。
# Gauge 会陈旧——进程启动时算成"还有 60 天"，过一个月它仍写着 60，告警就永久失灵。
# 上报"最新分区上界的 epoch 秒"后，由 Prometheus 用 `- time()` 做减法：**时钟自己走**，
# 即使应用再也不重算，告警也会随真实时间逼近而触发。
# 证据引用解析结果。为什么要有：`/v1/candidates/{id}/refs` 的失败此前**只是响应里的一句话**——
# 运维看不到 "execution_failed 在涨"（= rawlog 没配好/数据源崩），也看不到 "denied 在涨"
# （= 有人在探测越权）。outcome ∈ {ok, invalid_input, not_found, denied, unavailable,
# execution_failed, contract_mismatch}。
evidence_resolve_total = Counter(
    "data_dog_evidence_resolve_total",
    "证据引用解析次数（按结果分类）",
    labelnames=("outcome",),
    registry=registry,
)

partition_end_timestamp_seconds = Gauge(
    "data_dog_partition_end_timestamp_seconds",
    "最新分区上界的 epoch 秒（label kind=future；与 time() 相减 = 还有多久覆盖到期）",
    labelnames=("kind",),
    registry=registry,
)

# 单次异步调查的执行耗时（从 worker 领取到落终态）
investigation_job_duration_seconds = Histogram(
    "data_dog_investigation_job_duration_seconds",
    "异步调查执行耗时（秒）",
    # 覆盖离线毫秒级到真实 LLM 的分钟级（60s 硬超时 + 排队余量）
    buckets=JOB_DURATION_BUCKETS,
    registry=registry,
)

# 单次 **LLM 请求**耗时（一次 HTTP 往返；重试是上层显式做的）。
# 为什么单独一条：`http_request_duration_seconds` 量的是**端点**耗时，同步调查里"等服务端 LLM"
# 与"我们自己的编排"是两件事；排查"是模型慢还是我们慢"必须能分开看。
# 不另设 `llm_requests_total`：直方图自带的 `_count` 就是次数，避免指标重复。
LLM_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0)
llm_request_duration_seconds = Histogram(
    "data_dog_llm_request_duration_seconds",
    "单次 LLM 请求耗时（秒，按供应商/模型/结果）",
    labelnames=("vendor", "model", "outcome"),   # outcome: ok / timeout / unavailable
    buckets=LLM_LATENCY_BUCKETS,
    registry=registry,
)

# 租户 LLM 额度（治理）：超限次数按**阶段**分（接活前 / 跑一半），便于区分
# "还没花钱就被拦"与"已经花了一半被拦"。
llm_budget_exceeded_total = Counter(
    "data_dog_llm_budget_exceeded_total",
    "因租户 LLM 额度/花费耗尽被拦下的次数（按阶段）",
    labelnames=("stage",),
    registry=registry,
)
llm_budget_tokens_total = Counter(
    "data_dog_llm_budget_tokens_total",
    "记入租户额度的 token 数（**不带租户标签**，避免基数爆炸）",
    registry=registry,
)
llm_budget_usd_total = Counter(
    "data_dog_llm_budget_usd_total",
    "记入租户额度的估算花费（USD；按价格表估算，不是账单）",
    registry=registry,
)

# 审计写入失败次数（>0 必须告警：审计是只增证据，写不进去等于合规缺口；
# 但写入失败**不会**让业务请求失败，所以只能靠这个计数器被发现）
audit_write_failed = Counter(
    "data_dog_audit_write_failed_total",
    "安全审计写入失败次数（审计缺失=合规缺口，须告警）",
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

# ---------------------------------------------------------------- 车队级分片（P4-5）

# 因不归本副本而被跳过的消息数（客户端分片过滤的证据）。持续 >0 说明本副本在
# "让路"给属主；等量放大到全集群应≈总量（每键恰一个属主）。
shard_skipped_total = Counter(
    "data_dog_shard_skipped_total",
    "因分片归属非本副本而跳过的消息数",
    registry=registry,
)

# 本副本是否启用了客户端分片过滤（1=启用，0=未启用/单副本）
shard_enabled = Gauge(
    "data_dog_shard_enabled",
    "本副本是否启用客户端分片过滤（1=启用，0=未启用）",
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

# ---------------------------------------------------------------- 退化趋势预测（P4-7）

# 预测产出：outcome ∈ {predicted, no_eta}（no_eta=失败关闭，宁缺勿猜）。
forecast_total = Counter(
    "data_dog_forecast_total",
    "退化趋势预测次数（按是否给出 ETA 分类）",
    labelnames=("outcome",),
    registry=registry,
)

# 预测置信度分布（方向一致性 0..1），用于观察预测质量
forecast_confidence = Histogram(
    "data_dog_forecast_confidence",
    "退化趋势预测的置信度（方向一致性）",
    buckets=(0.1, 0.3, 0.5, 0.7, 0.8, 0.9, 0.95, 1.0),
    registry=registry,
)


# ---------------------------------------------------------------- 跨地域复制（P4-6）

# 复制延迟：从上游区域最新已复制事务到本区域可见的时差（秒）。
# from/to 用区域标识，基数受控（区域数有限，非用户输入）。
replication_lag_seconds = Gauge(
    "data_dog_replication_lag_seconds",
    "跨区域复制延迟（秒）——最后一次已复制变更的时间差",
    labelnames=("from", "to"),
    registry=registry,
)

# 冲突消解计数：按结果分类（causal=按 revision 因果序收敛 /
# concurrent=真并发按策略收敛）。用于观察双向写冲突的频率与形态。
replication_conflicts_total = Counter(
    "data_dog_replication_conflicts_total",
    "冲突消解次数（按消解路径分类）",
    labelnames=("path",),
    registry=registry,
)


def observe_replication_lag(frm: str, to: str, lag_s: float) -> None:
    """记录一次复制延迟（from=上游区域，to=本区域）。"""
    replication_lag_seconds.labels(frm=frm, to=to).set(max(0.0, lag_s))


def observe_conflict(path: str) -> None:
    """记录一次冲突消解。path ∈ {causal, concurrent}。"""
    replication_conflicts_total.labels(path=path).inc()


def observe_forecast(has_eta: bool, confidence: float) -> None:
    """记录一次预测：是否给出 ETA + 置信度。"""
    forecast_total.labels(outcome="predicted" if has_eta else "no_eta").inc()
    forecast_confidence.observe(max(0.0, min(1.0, confidence)))


def observe_llm_request(vendor: str, model: str, outcome: str, duration_s: float) -> None:
    """记一次 LLM 请求。**观测失败绝不影响调用**——指标是旁路，不是链路的一环。"""
    try:
        llm_request_duration_seconds.labels(
            vendor=vendor or "?", model=model or "?", outcome=outcome or "?"
        ).observe(max(0.0, float(duration_s)))
    except Exception:                                # noqa: BLE001 - 见 docstring
        pass


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
