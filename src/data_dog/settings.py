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
