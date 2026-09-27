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


def default_tenant() -> str:
    """无租户上下文时使用的租户（demo/CLI/迁移回填共用此值）。"""
    return _get("DD_DEFAULT_TENANT", default="demo")


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
