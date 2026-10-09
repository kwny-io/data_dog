"""集中配置：所有环境变量的**唯一读取点**。

为什么要集中
------------
此前 `os.environ.get("DD_...")` 散落在十几个模块里，同一个语义可能出现
两种前缀（`DD_*` 与 `DATA_DOG_*`）——排查"这个值从哪来"要全仓搜。

设计取舍
--------
- **零新依赖**：纯 `os.environ` 读取，不引入 pydantic-settings。
- **调用时读取，绝不缓存**：测试大量使用 `monkeypatch.setenv` 后再调用，
  若在 import 时求值会全部失效。因此每个函数每次调用都重新读环境。
- **别名兼容**：`DATA_DOG_VENDOR` / `DATA_DOG_MODEL` 是历史前缀，保留为
  别名；规范名迁移到 `DD_LLM_VENDOR` / `DD_LLM_MODEL`。
"""
from __future__ import annotations

import os


def _get(canonical: str, *aliases: str, default: str = "") -> str:
    """按顺序取第一个**非空**的环境变量值；都没有则返回 default。

    空串视为未设置（与项目里 `os.environ.get(x, "").strip() or default` 的
    既有惯例一致）——避免 `DD_X=""` 意外覆盖默认值。
    """
    for name in (canonical, *aliases):
        v = os.environ.get(name, "").strip()
        if v:
            return v
    return default


def _get_float(canonical: str, *aliases: str, default: float) -> float:
    raw = _get(canonical, *aliases)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(canonical: str, *aliases: str, default: int) -> int:
    raw = _get(canonical, *aliases)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_bool(canonical: str, *aliases: str, default: bool) -> bool:
    """布尔语义：显式 falsy 串判假，其余非空判真。"""
    raw = _get(canonical, *aliases)
    if not raw:
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


# ---------------------------------------------------------------- 服务

def service_name() -> str:
    return _get("DD_SERVICE_NAME", default="data-dog")


def service_host() -> str:
    return _get("DD_SERVICE_HOST", default="0.0.0.0")


def service_port() -> int:
    return _get_int("DD_SERVICE_PORT", default=8000)


def log_level() -> str:
    return _get("DD_LOG_LEVEL", default="INFO")


def log_json() -> bool:
    return _get_bool("DD_LOG_JSON", default=True)


def api_key() -> str:
    """对外暴露时**必须**设置的静态密钥；空串 = 不校验（本地开发）。"""
    return _get("DD_API_KEY")


def cors_origins() -> list[str]:
    """允许的跨源来源（逗号分隔）。空列表 = 不挂载 CORS 中间件（同源）。"""
    raw = _get("DD_CORS_ORIGINS")
    return [o.strip() for o in raw.split(",") if o.strip()]


# ---------------------------------------------------------------- 数据库

def database_url() -> str:
    """连接串；未设则本地 SQLite（由 storage.db 兜底具体路径）。"""
    return _get("DD_DATABASE_URL")


def auto_migrate() -> bool:
    """服务启动时是否自动 `alembic upgrade head`。生产多副本应设 0。"""
    return _get_bool("DD_AUTO_MIGRATE", default=True)


# 为什么这三个超时默认 5s（而不是库默认的"无限/30s"）：见 storage/db.py 的
# `engine_kwargs` docstring。要点是——建连/拿连接的等待必须有上界，且上界要与
# 本仓其它"快速失败"预算同量级（redis 状态冲突重试 5s、通知 HTTP 5s），
# 这样"数据库慢了"会以显式错误暴露，而不是把请求线程静默钉住。

def db_connect_timeout_s() -> float:
    """建连超时（秒，仅 PostgreSQL 生效）：TCP 建连 + 启动握手的总预算。

    libpq 默认 `connect_timeout=0` = **无限等**（最终靠 OS 的 TCP 超时，可达分钟级），
    故必须显式给值。有上限之后，"连不上"会以 `OperationalError`（connection timeout
    expired）快速浮出来，而不是让请求线程在 SYN 重传里无声等待。
    """
    return max(0.1, _get_float("DD_DB_CONNECT_TIMEOUT_S", default=5.0))


def db_pool_timeout_s() -> float:
    """从连接池取连接的最长等待（秒，仅 PostgreSQL 生效）。

    SQLAlchemy 默认 30s：池被占满时请求会**静默排队半分钟**再报错。改成 5s 是
    "服务器过载就快速失败"的选择——快速 503 比慢 200 更早暴露容量问题。
    """
    return max(0.1, _get_float("DD_DB_POOL_TIMEOUT_S", default=5.0))


def db_statement_timeout_ms() -> int:
    """单条语句最长时间（毫秒，仅 PostgreSQL 生效）；**0 = 不限**。

    服务端 `statement_timeout`：卡死的查询/被锁住的写不会永久占着连接。默认 15s
    远高于应用查询的量级（docs/slo.md 的热路径预算是毫秒级），只用来兜住"病态慢"。
    注意：alembic 迁移走自己的引擎（storage/migrations.py），不受此值约束，
    长迁移不会被误杀。
    """
    return max(0, _get_int("DD_DB_STATEMENT_TIMEOUT_MS", default=15_000))


# ---------------------------------------------------------------- 共享状态

def state_backend() -> str:
    """跨副本共享状态后端：memory / postgres / redis。空=按数据库类型自动选。"""
    return _get("DD_STATE_BACKEND").lower()


def rate_backend() -> str:
    """限流后端（可独立于状态后端）。空 = 跟随 `DD_STATE_BACKEND`。

    拆分场景（限流检查成为热路径瓶颈时）：`DD_RATE_BACKEND=redis` 让限流走
    Redis（sub-ms + 原生 TTL），业务状态仍走 PG（耐久 + 可查询）。默认不拆，
    两者同源，少运维一个组件。
    """
    return _get("DD_RATE_BACKEND").lower()


def redis_url() -> str:
    """Redis 连接串（DD_STATE_BACKEND=redis 时需要）。"""
    return _get("DD_REDIS_URL", default="redis://localhost:6379/0")


# ---------------------------------------------------------------- 状态快照（G3）

def checkpoint_enabled() -> bool:
    """是否启用周期状态快照（默认关；多副本/需恢复时开）。"""
    return _get_bool("DD_CHECKPOINT_ENABLED", default=False)


def checkpoint_interval_s() -> float:
    """周期快照间隔（秒）。也是恢复的 RPO 上界（两次快照间重启会丢这段增量）。"""
    return max(5.0, _get_float("DD_CHECKPOINT_INTERVAL_S", default=300.0))


def checkpoint_keep() -> int:
    """保留最近多少份快照（防表无限增长）。"""
    return max(1, _get_int("DD_CHECKPOINT_KEEP", default=5))


def checkpoint_restore() -> bool:
    """启动时是否从最新快照恢复状态。"""
    return _get_bool("DD_CHECKPOINT_RESTORE", default=False)


def checkpoint_prefix() -> str:
    """快照只导出此前缀匹配的 key（空 = 全部状态）。"""
    return _get("DD_CHECKPOINT_PREFIX")


def ops_token() -> str:
    """运维端点令牌（`X-Ops-Token`）。空 = 运维端点不暴露（404）。

    状态快照/diff 涉及各租户算子状态，属敏感数据，故**独立**于 `DD_API_KEY`
    门控——即便开了 API 鉴权，运维端点也只对持 ops token 者开放。
    """
    return _get("DD_OPS_TOKEN")


# ---------------------------------------------------------------- 流式接入

def stream_enabled() -> bool:
    """是否在进程内启动流式接入消费者。"""
    return _get_bool("DD_STREAM_ENABLED", default=False)


def stream_shards() -> int:
    """流式消费者的**副本数**（K8s 里 `deployment-stream` 的 replicas）。

    这个值**不在消费进程内决定并行度**，而是为了让进程自检：
    file 源（单文件、无协调）配了多副本 → 启动即失败关闭（见 stream/factory.py），
    免得多个副本各尾随一份、事件被重复处理而无人察觉。
    多副本只对 kafka（消费者组）/ mqtt（共享订阅）安全。
    """
    return max(1, _get_int("DD_STREAM_SHARDS", default=1))


def stream_source() -> str:
    """流式源类型：file / http / mqtt / kafka。"""
    return _get("DD_STREAM_SOURCE", default="file").lower()


def stream_tenant() -> str:
    """流式信道的租户（身份来源）。本地文件/总线不带应用层身份，只能从配置取。"""
    return _get("DD_STREAM_TENANT", default="demo")


def stream_device_sn() -> str:
    """流式信道的默认设备号（正文取不到 device 时的兜底）。"""
    return _get("DD_STREAM_DEVICE_SN")


def stream_device_from_field() -> str:
    """从每条消息正文的该字段取 device_sn（一个信道承载多台设备时用）。"""
    return _get("DD_STREAM_DEVICE_FIELD")


def stream_http_host() -> str:
    return _get("DD_STREAM_HTTP_HOST", default="127.0.0.1")


def stream_http_port() -> int:
    return _get_int("DD_STREAM_HTTP_PORT", default=8099)


def stream_http_path() -> str:
    return _get("DD_STREAM_HTTP_PATH", default="/ingest")


def stream_file_path() -> str:
    """file 源：尾随的日志文件/目录路径。"""
    return _get("DD_STREAM_FILE_PATH")


def stream_poll_ms() -> int:
    """file 源：轮询间隔（毫秒）。"""
    return _get_int("DD_STREAM_POLL_MS", default=500)


def stream_mqtt_url() -> str:
    return _get("DD_STREAM_MQTT_URL", default="tcp://localhost:1883")


def stream_mqtt_topic() -> str:
    return _get("DD_STREAM_MQTT_TOPIC", default="telemetry/#")


def stream_kafka_bootstrap() -> str:
    return _get("DD_STREAM_KAFKA_BOOTSTRAP", default="localhost:9092")


def stream_kafka_topic() -> str:
    return _get("DD_STREAM_KAFKA_TOPIC", default="telemetry")


def stream_kafka_group() -> str:
    return _get("DD_STREAM_KAFKA_GROUP", default="data-dog")


# ---------------------------------------------------------------- 车队级分片（P4-5）

def stream_replica_id() -> str:
    """本副本的 id（分片路由用）。缺省空串——单副本场景无需配。"""
    return _get("DD_REPLICA_ID")


def stream_replica_members() -> list[str]:
    """集群里所有消费者副本 id（逗号分隔）。所有副本必须看到**相同**的列表，
    否则 rendezvous 归属判定不一致 → 丢事件。单副本留空即可。"""
    raw = _get("DD_REPLICA_MEMBERS")
    return [m.strip() for m in raw.split(",") if m.strip()] if raw else []


def stream_shard_count() -> int:
    """分片总数（远大于副本数，便于均衡与再平衡）。"""
    return max(1, _get_int("DD_SHARD_COUNT", default=256))


# ---------------------------------------------------------------- 跨地域多活（P4-6）

def region() -> str:
    """本实例所在区域标识（如 `cn-east` / `cn-north`）。

    用于度量与运维归属（复制延迟标签 `from`/`to`）。缺省空串=未分区（单区部署，
    与现状行为一致）。**不参与**数据路由——路由按租户/车队就近归属，是编排层决策。
    """
    return _get("DD_REGION")


def replication_source() -> str:
    """本实例的复制**上游**区域（订阅谁的变更）。空串=非复制部署。

    只作部署标注与可观测性用途；真正的复制由 PG 逻辑复制 / CDC 承担
    （见 `docs/p4/06_multi_region.md`），**非应用代码**。
    """
    return _get("DD_REPLICATION_SOURCE")


def replication_lag_warn_s() -> float:
    """复制延迟告警阈值（秒）：`dd_replication_lag_seconds` 超过即应告警。

    异步复制下延迟本就 >0；此阈值界定"可接受 vs 需介入"，供 runbook 使用。
    """
    return max(0.0, _get_float("DD_REPLICATION_LAG_WARN_S", default=30.0))


# ---------------------------------------------------------------- 多副本协调（G5）

def stream_mqtt_share_group() -> str:
    """MQTT 共享订阅组名：拼成 `$share/<group>/<topic>`，broker 在组内轮询投递。

    空串 = 不用共享订阅（退回"每个副本收全量"——单副本可接受，多副本会重复）。
    """
    return _get("DD_MQTT_SHARE_GROUP", default="data-dog")


def stream_file_lease_ttl_s() -> float:
    """file 源租约 TTL（秒）：持有者崩溃后至多这么久被旁人接管。"""
    return max(1.0, _get_float("DD_STREAM_LEASE_TTL_S", default=30.0))


def stream_share_offset() -> bool:
    """file 源偏移是否共享（接管者从上次共享偏移续读，而非从头/从尾）。"""
    return _get_bool("DD_STREAM_SHARE_OFFSET", default=True)


# ---------------------------------------------------------------- 限流

def rate_per_sec() -> float:
    return _get_float("DD_RATE_PER_SEC", default=200.0)


def rate_burst() -> int:
    return _get_int("DD_RATE_BURST", default=400)


# ---------------------------------------------------------------- LLM

def llm_vendor() -> str:
    return _get("DD_LLM_VENDOR", "DATA_DOG_VENDOR", default="deepseek")


def llm_model() -> str:
    return _get("DD_LLM_MODEL", "DATA_DOG_MODEL")


def llm_timeout_s() -> float | None:
    """LLM 调用超时（秒）。0 或负值 = 不限时（不推荐）。未设 = 60s。"""
    raw = _get("DD_LLM_TIMEOUT_S")
    if not raw:
        return 60.0
    try:
        v = float(raw)
    except ValueError:
        return 60.0
    return None if v <= 0 else v


# ---------------------------------------------------------------- 调查端点

def investigate_timeout_s() -> float:
    return _get_float("DD_INVESTIGATE_TIMEOUT_S", default=90.0)


def investigate_max_steps() -> int:
    return _get_int("DD_INVESTIGATE_MAX_STEPS", default=8)


def investigate_offline() -> bool:
    """强制用离线确定性 provider（CI/测试用，不调真实 LLM）。"""
    return _get_bool("DD_INVESTIGATE_OFFLINE", default=False)


def investigate_async() -> bool:
    """`POST /v1/investigations` 默认是否异步（提交→202 job 句柄）。

    默认 **False**：保持既有同步语义（前端按同步长任务处理）。置 1 后默认走异步，
    调用方仍可用 `?sync=1` 单独退回同步。异步化是把 worker 从长 LLM 调用里解放出来
    的关键；前端改用轮询后即可默认打开（见 docs/p4/gaps/01_async_investigation.md）。
    """
    return _get_bool("DD_INVESTIGATE_ASYNC", default=False)


def investigate_workers() -> int:
    """异步调查 worker 池大小（进程内线程数）。"""
    return max(1, _get_int("DD_INVESTIGATE_WORKERS", default=4))


def investigate_queue_poll_ms() -> int:
    """worker 空转时查共享队列的间隔（毫秒）。"""
    return max(20, _get_int("DD_INVESTIGATE_QUEUE_POLL_MS", default=200))


# ---------------------------------------------------------------- 事件时间（G2）

def allowed_lateness_s() -> float:
    """合并窗口允许的迟到容忍（秒）。事件早于 `watermark`（=最大事件时间-本值）即拒。

    同时也是恢复的 RPO 无关量：纯在线窗口参数。默认 30s——太小丢迟到数据（漏合并），
    太大增内存/延迟，按真实到达分布调。
    """
    return max(0.0, _get_float("DD_ALLOWED_LATENESS_S", default=30.0))


def watermark_enabled() -> bool:
    """是否启用事件时间水位线（有界迟到合并 + 窗口按事件时间有序）。

    默认 **关**：既有多数调用点未传事件时间，开启会改变合并边界；开启后窗口本就按
    事件时间排序（纯增益），合并从"无界 abs"收紧为"有向 + 水位线"。
    """
    return _get_bool("DD_WATERMARK_ENABLED", default=False)


# ---------------------------------------------------------------- 变更管理源（G7）

def change_source() -> str:
    """变更来源：mock（默认，内置表）/ jsonl（文件）/ webhook（CMDB API）。"""
    return _get("DD_CHANGE_SOURCE", default="mock").lower()


def change_jsonl_path() -> str:
    """jsonl 来源的变更记录文件路径。"""
    return _get("DD_CHANGE_JSONL_PATH")


def change_webhook_url() -> str:
    """webhook 来源的 CMDB API 地址（请求前过 SSRF 校验）。"""
    return _get("DD_CHANGE_WEBHOOK_URL")


def change_fallback_demo() -> bool:
    """变更源**不可用**时是否回退内置样例变更表（默认关；只给本地演示用）。

    为什么默认关：内置表是样例数据。把"查不到"悄悄变成"一份样例变更列表"，会让
    Agent 的"近期无（相关）变更"结论建立在假数据上——这与本仓"拿不准就失败关闭"
    的口径冲突。默认行为改为向 Agent 返回 `unavailable` 错误（不含 changes 字段），
    要演示"降级"这条路径必须显式打开本开关，且回退结果会带 `degraded=True` 标注。
    """
    return _get_bool("DD_CHANGE_FALLBACK_DEMO", default=False)


# ---------------------------------------------------------------- 因果时序（G6）

def causal_max_lag_s() -> float:
    """疑似因果链允许的最大滞后（秒）：因必须早于果且滞后在窗内。

    默认 5s——电气/热传播时标不同，按机型调；太小漏关联、太大引入伪因果。
    """
    return max(0.0, _get_float("DD_CAUSAL_MAX_LAG_S", default=5.0))


def causal_enabled() -> bool:
    """是否在证据桥启用因果链查询（默认开：查询只读、零副作用）。"""
    return _get_bool("DD_CAUSAL_ENABLED", default=True)


# ---------------------------------------------------------------- 降级队列（G4）

def degradation_queue_backend() -> str:
    """降级队列后端：memory（默认，进程内）/ state（共享状态，重启可续 + 多副本共享）。

    `state` 要真跨进程持久/共享，`DD_STATE_BACKEND` 须落在 postgres/redis——
    memory 状态后端只在同进程内共享。
    """
    return _get("DD_DEGRADATION_QUEUE", default="memory").lower()


# ---------------------------------------------------------------- 算子沙箱（P4-1）

def sandbox_kind() -> str:
    """算子执行器类型：inline（默认，单进程+预算）/ subprocess（进程隔离+资源上限）。

    wasm 为预留档位（未实现）。热路径保持 inline；对**不可信/待审**算子或隔离有
    硬要求的部署改 subprocess。见 `docs/p4/01_sandbox.md`。
    """
    return _get("DD_SANDBOX_KIND", default="inline").lower()


def sandbox_wall_s() -> float:
    """单算子墙钟上限（秒）。默认 50ms，对齐 slo.md §4（热路径须极短）。"""
    return _get_float("DD_SANDBOX_WALL_S", default=0.05)


def sandbox_max_steps() -> int:
    """解释器步数预算（Tier 0）。默认 10 万，防超长表达式。"""
    return _get_int("DD_SANDBOX_MAX_STEPS", default=100_000)


def sandbox_mem_mb() -> int:
    """子进程地址空间增量上限（MB，Tier 1）。默认 64。"""
    return _get_int("DD_SANDBOX_MEM_MB", default=64)


def sandbox_cpu_s() -> float:
    """子进程 CPU 时间上限（秒，Tier 1 rlimit）。默认 0.1。"""
    return _get_float("DD_SANDBOX_CPU_S", default=0.1)


def sandbox_policy():
    """组装 `SandboxPolicy`（延迟导入避免 settings 依赖 operator_factory）。"""
    from .operator_factory.sandbox import SandboxPolicy
    return SandboxPolicy(
        wall_s=sandbox_wall_s(),
        max_steps=sandbox_max_steps(),
        mem_mb=sandbox_mem_mb(),
        cpu_s=sandbox_cpu_s(),
        kind=sandbox_kind(),
    )


# ---------------------------------------------------------------- 算子控制面（P4-2）

def operator_signing_key() -> str:
    """算子制品签名密钥（HMAC 对称密钥）。**不进仓库**，生产环境变量注入。

    空 = 未配置：注册端点将拒绝带签名的注册（失败关闭）。本项提供签名机制，
    **不提供 KMS/HSM**——密钥托管是运营职责（见 docs/p4/02_control_plane.md §6）。
    """
    return _get("DD_OPERATOR_SIGNING_KEY")


def operator_pubkey() -> str:
    """Ed25519 公钥（hex）。与签名密钥二选一：配了它则用非对称验签（验签方无需私钥）。"""
    return _get("DD_OPERATOR_PUBKEY")


def operator_sign_alg() -> str:
    """签名算法：hmac-sha256（默认，零依赖）/ ed25519（需可选依赖 cryptography）。"""
    return _get("DD_OPERATOR_SIGN_ALG", default="hmac-sha256").lower()


def canary_min_samples() -> int:
    """看门狗最小样本数：低于此值**不自动判定**（冷启动保守，见 P4-2 §6）。"""
    return max(1, _get_int("DD_CANARY_MIN_SAMPLES", default=50))


def canary_max_crash_rate() -> float:
    return _get_float("DD_CANARY_MAX_CRASH_RATE", default=0.02)


def canary_max_parse_drop() -> float:
    return _get_float("DD_CANARY_MAX_PARSE_DROP", default=0.05)


def canary_max_candidate_multiple() -> float:
    return _get_float("DD_CANARY_MAX_CANDIDATE_MULTIPLE", default=3.0)


# ---------------------------------------------------------------- 退化趋势预测（P4-7）

def forecast_horizon_h() -> float:
    """预测视界（小时）：ETA 超过它不给数——外推越远越不可信。默认 24h。"""
    return max(0.0, _get_float("DD_FORECAST_HORIZON_H", default=24.0))


def forecast_min_conf() -> float:
    """方向一致性门槛（0..1）：低于它不预测（宁缺勿猜）。默认 0.5。"""
    return _get_float("DD_FORECAST_MIN_CONF", default=0.5)


def forecast_min_samples() -> int:
    """最少样本数：不足不预测。默认 6。"""
    return max(1, _get_int("DD_FORECAST_MIN_SAMPLES", default=6))


def forecast_thresholds() -> dict[str, float]:
    """各信号的目标阈值，形如 `temperature:95,current:12`（逗号分隔的 `信号:阈值`）。

    预测「到达该阈值」的 ETA；无阈值的信号不预测（没有目标就无从谈到达）。默认给
    温度一个保守上限示例——真实阈值应随机型标定。
    """
    raw = _get("DD_FORECAST_THRESHOLDS", default="temperature:95")
    out: dict[str, float] = {}
    for part in raw.split(","):
        if ":" not in part:
            continue
        k, _, v = part.partition(":")
        k = k.strip()
        try:
            out[k] = float(v.strip())
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------- 通知 / 值班 / 升级（P4-3）

def notify_enabled() -> bool:
    """是否在调查产事件后**主动投递通知**（默认关）。

    默认关：既有部署的告警流止于 SaaS adapter（落库审计）；开启后才会按值班表
    派发到 webhook/邮件/IM。开启即意味着"会真的往外发消息"，故默认不干扰。
    """
    return _get_bool("DD_NOTIFY_ENABLED", default=False)


def notify_ack_sla_s() -> float:
    """一线（level=1）确认 SLA（秒）：超此未 ack → 升级到下一级。默认 300s。"""
    return max(1.0, _get_float("DD_NOTIFY_ACK_SLA_S", default=300.0))


def notify_sla_l2_s() -> float:
    """二线（level=2）SLA（秒）。默认 600s。"""
    return max(1.0, _get_float("DD_NOTIFY_SLA_L2_S", default=600.0))


def notify_sla_l3_s() -> float:
    """三线/专家（level=3）SLA（秒）。默认 900s。"""
    return max(1.0, _get_float("DD_NOTIFY_SLA_L3_S", default=900.0))


def notify_max_level() -> int:
    """升级链最高级（含）。到顶仍无 ack → 该投递链记为 failed（不无限升级）。默认 3。"""
    return max(1, _get_int("DD_NOTIFY_MAX_LEVEL", default=3))


def notify_sla_for_level(level: int) -> float:
    """按级取 SLA；>3 级退回 level-1 的默认档（stay bounded）。"""
    table = {1: notify_ack_sla_s(), 2: notify_sla_l2_s(), 3: notify_sla_l3_s()}
    return table.get(level, notify_sla_l3_s())


def notify_sweep_interval_s() -> float:
    """升级 sweeper 的扫描间隔（秒）。默认 15s。"""
    return max(1.0, _get_float("DD_NOTIFY_SWEEP_INTERVAL_S", default=15.0))


def notify_http_timeout_s() -> float:
    """webhook/IM 通知的 HTTP 超时（秒）。默认 5s。"""
    return max(0.1, _get_float("DD_NOTIFY_HTTP_TIMEOUT_S", default=5.0))


def smtp_timeout_s() -> float:
    """SMTP 通知超时（秒）。默认 5s（与 HTTP 通知同量级，但**可各自调**）。

    为什么要单独一项：此前 `EmailNotifier` 直接复用 `DD_NOTIFY_HTTP_TIMEOUT_S`，
    于是"把邮件超时调长"会连带放长 webhook 超时（反之亦然）。两类下游的特性完全不同
    （SMTP 要 TLS 握手 + 认证 + 中继排队，webhook 是一次 HTTP POST），必须能分开设。
    默认值与旧行为一致（5s），故属于"解耦"而非"改行为"。
    """
    return max(0.1, _get_float("DD_SMTP_TIMEOUT_S", default=5.0))


def notify_allow_private_targets() -> bool:
    """是否允许通知目标指向内网/回环地址。

    默认 **False**（失败关闭）：webhook/IM 目标过 `data_plane.fetch.validate_url`
    的 SSRF 校验，内网 URL 被拒——**别让"通知"变成 SSRF 入口**。仅本地测试/受信
    内网部署才置 1。邮件（SMTP）目标不是 URL，不受此开关约束。
    """
    return _get_bool("DD_NOTIFY_ALLOW_PRIVATE", default=False)


def smtp_host() -> str:
    """SMTP 服务器（EmailNotifier 用）。空 = 未配（邮件通道不可用）。"""
    return _get("DD_SMTP_HOST")


def smtp_port() -> int:
    return _get_int("DD_SMTP_PORT", default=587)


def smtp_user() -> str:
    """SMTP 用户名/发件地址。**凭据走环境变量，不进仓库**。"""
    return _get("DD_SMTP_USER")


def smtp_password() -> str:
    return _get("DD_SMTP_PASSWORD")


def smtp_from() -> str:
    """发件人（From）。空则退回 `smtp_user`。"""
    return _get("DD_SMTP_FROM") or smtp_user()


def im_webhook_url() -> str:
    """IM 群机器人（飞书/钉钉/企微）webhook URL。空 = IM 通道不可用。

    走 SSRF 校验（与 webhook 同）。凭据在 URL 里，**不进仓库**，环境变量注入。
    """
    return _get("DD_IM_WEBHOOK_URL")


# ---------------------------------------------------------------- 路径

def project_root() -> str | None:
    return _get("DD_PROJECT_ROOT") or None


def vendor_docs_dir() -> str | None:
    return _get("DD_VENDOR_DOCS") or None


def operators_dir() -> str | None:
    return _get("DD_OPERATORS_DIR") or None


def data_dir() -> str | None:
    return _get("DD_DATA_DIR") or None


# ---------------------------------------------------------------- 知识库 / MCP

def kb_collection() -> str:
    return _get("DD_KB_COLLECTION", default="unitree_go2")


def kb_chroma() -> str:
    return _get("DD_KB_CHROMA")


def chroma_mcp_exe() -> str:
    return _get("DD_CHROMA_MCP_EXE")


def ctx_mcp() -> str:
    return _get("DD_CTX_MCP")


def ctx_mcp_server() -> str:
    return _get("DD_CTX_MCP_SERVER")
