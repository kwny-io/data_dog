"""数据库引擎 / 会话管理（SQLAlchemy 2.0）。

- 默认 SQLite（本地零安装）；设 `DD_DATABASE_URL` 切 PostgreSQL，代码不改。
- SQLite 需要 `check_same_thread=False`（多线程/测试）；PostgreSQL 用连接池。
- PostgreSQL 的**建连 / 拿连接 / 单条语句**三段等待都有显式上限（见 `engine_kwargs`）：
  数据库不可用或过载时必须快速失败并报错，而不是静默把请求线程钉住几十秒。
- **池容量也是并发上限**（`DD_DB_POOL_SIZE` / `DD_DB_MAX_OVERFLOW`）：库默认 5+10=15，
  实测在 60 并发写下会让吞吐停在 15 并发的水平、P99 却差 6.1 倍（见 `settings.db_pool_size`）。
- SQLite 每条连接应用 WAL / `synchronous=NORMAL` / `busy_timeout`（见 `_apply_sqlite_pragmas`），
  默认 rollback journal 下"每次 COMMIT 一次 fsync + 写阻塞读"是单机写入只有 36 req/s 的主因。
- 提供上下文管理器 `session_scope()`：自动 commit / rollback / close。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..paths import data_dir

_log = logging.getLogger(__name__)

# 当前请求的租户上下文（RLS 用）。由服务中间件/调用方设置；`session_scope`
# 读取它并在事务内注入 PG 的 `app.tenant_id`。用 ContextVar 而非线程局部：
# 与 async/并发模型一致，且请求结束即失效。
_current_tenant: ContextVar[str | None] = ContextVar("dd_tenant", default=None)


def set_tenant_context(tenant_id: str | None):
    """设置当前上下文的租户。返回 token，供 reset 还原（中间件用）。"""
    return _current_tenant.set(tenant_id or None)


def reset_tenant_context(token) -> None:
    _current_tenant.reset(token)


def current_tenant() -> str | None:
    return _current_tenant.get()


def _default_sqlite_url() -> str:
    """本地 SQLite 默认位置（运行时解析，可用 DD_DATA_DIR 覆盖）。"""
    return f"sqlite:///{(data_dir() / 'data_dog.db').as_posix()}"

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None
# 保护 (_engine, _SessionLocal) 这对语义绑定的**一致**创建与清空。
#
# 真实踩到的故障：异步调查 worker 是后台线程，关停用 `stop(wait=False)` 不等其退出。
# 于是在「上一测试收尾（monkeypatch 回滚了 DD_DATABASE_URL）→ 下一测试 reset_engine()
# 之间」的空窗期，残留 worker 仍可能调 get_engine()：此时 env 为空 → 引擎落到**默认库**
# data_dog.db，并把这份错误引擎写进全局单例 → 下一测试的 session_scope() 连到该空库，
# 报随机的 `no such table`。根治靠测试侧排干线程（见 tests/conftest.py）；此处加锁是
# **纵深防御**：保证并发创建/清空下不会出现 `_engine` 与 `_SessionLocal` 撕裂的状态。
_engine_lock = threading.Lock()


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def database_url() -> str:
    """数据库连接串：优先环境变量 `DD_DATABASE_URL`，否则本地 SQLite。"""
    from .. import settings
    return settings.database_url() or _default_sqlite_url()


def get_engine(url: str | None = None) -> Engine:
    """获取（并缓存）引擎。传入 url 可覆盖（测试用内存库）。"""
    global _engine, _SessionLocal
    target = url or database_url()
    if url is not None:
        # 临时引擎：不缓存、不碰全局，无需持锁（无共享写）。
        return _make_engine(target)
    with _engine_lock:
        if _engine is None:
            engine = _make_engine(target)
            _engine = engine
            _SessionLocal = sessionmaker(
                bind=engine, expire_on_commit=False, future=True)
        return _engine


def engine_kwargs(target: str) -> dict[str, Any]:
    """按 URL 方言构造 `create_engine` 的 kwargs(**唯一实现**，`_make_engine` 与
    `state/pg.py` 共用，避免两处各写一套而漂移)。

    为什么 PostgreSQL 必须显式给三个时间上限
    ----------------------------------------
    "建连/拿连接/跑语句"三段等待，库默认值分别是**无限**（libpq `connect_timeout=0`）、
    30s（SQLAlchemy `pool_timeout`）、**无限**（PG `statement_timeout=0`）。三者任一
    卡住都会把**请求线程**钉在原地：HTTP 层表现为超时/挂起，后台 worker 表现为
    队列堆积，而且日志里往往只看到"没有响应"，定位不到"在等数据库"。

    这与本仓"拿不准就快速失败（fail-closed）"是同一口径：数据库不可用/过载时，
    我们要的是**明确的 503/报错**（可告警、可重试、可让上游降级），而不是一个
    悄悄挂住 30 秒~几分钟的连接尝试。阈值取 5s 与仓库内其它"快速失败"预算同量级
    （redis 状态冲突重试 5s、通知 HTTP 5s），使一次数据库等待不会长过业务自身的预算。

    SQLite **刻意不引入**这些参数：本地单文件库没有网络建连、默认`QueuePool` 也不
    会被远端占满，给它加 `pool_timeout`/`connect_timeout` 只会改变既有本地行为
    （测试与本机开发都依赖该行为不变）。SQLite 的性能调优走**连接级 PRAGMA**
    （见 `_apply_sqlite_pragmas`），不在这里。
    """
    from .. import settings

    kwargs: dict[str, Any] = {"future": True}
    if target.startswith("sqlite"):
        # SQLite：允许跨线程 + 不要求严格类型（行为与既有版本逐字节一致）
        kwargs["connect_args"] = {"check_same_thread": False}
        return kwargs

    # PostgreSQL 等网络数据库：连接预检 + 三段等待全部有上限。
    kwargs["pool_pre_ping"] = True          # 防陈旧连接（池里被对端掐掉的连接）
    connect_args: dict[str, Any] = {
        "connect_timeout": settings.db_connect_timeout_s(),
    }
    statement_ms = settings.db_statement_timeout_ms()
    if statement_ms > 0:
        # 走 libpq 的 `options`（等价于会话级 `SET statement_timeout`）：
        # server 端计时，卡死的语句由 PG 主动取消（psycopg 抛 QueryCanceled），
        # 比客户端"看着像挂住"更能失败关闭。0 = 不限，此时**不加**该参数。
        #
        # 与 URL 里已有的 `?options=...` **合并**而不是覆盖：运维可能用它传
        # `-c search_path=...` 之类；`create_engine` 的 connect_args 优先级高于 URL
        # 查询串，直接写单值会把它静默丢掉（又一个"没报错但行为变了"）。
        connect_args["options"] = _merge_pg_options(
            _url_options(target), f"-c statement_timeout={statement_ms}")
    kwargs["connect_args"] = connect_args
    kwargs["pool_timeout"] = settings.db_pool_timeout_s()
    # 池容量 = 并发上限。库默认 5+10=15，实测在 60 并发写下"吞吐与 15 并发相同、
    # P99 差 6.1 倍"（多出来的线程全在池门口排队）——见 db_pool_size 的 docstring。
    # 注意：这是**池不够用**，与上面三项"等待有上限"是两件事，两者都要。
    kwargs["pool_size"] = settings.db_pool_size()
    kwargs["max_overflow"] = settings.db_max_overflow()
    return kwargs


def _url_options(target: str) -> str:
    """取连接串查询参数里的 `options=`（libpq 启动参数）；没有/解析不了 → 空串。

    解析失败不在这里报错：URL 本身有问题应由 `create_engine` 给出权威报错，
    本函数只负责"尽量不丢运维已写的 options"。
    """
    from sqlalchemy.engine import make_url
    try:
        return str(make_url(target).query.get("options", "") or "")
    except Exception:                        # noqa: BLE001 - 见 docstring
        return ""


def _merge_pg_options(existing: str, extra: str) -> str:
    """把 `extra` 追加到已有 libpq options 串后（空格分隔，去首尾空白）。"""
    return f"{existing.strip()} {extra}".strip()


def _apply_sqlite_pragmas(engine: Engine) -> None:
    """SQLite：每条新连接上应用 PRAGMA（WAL / synchronous=NORMAL / busy_timeout）。

    为什么用 `connect` 事件而不是 `engine_kwargs`：这些不是 `create_engine` 的参数，
    而是**每条连接**的会话设置。`engine_kwargs` 的 SQLite 分支必须保持**逐字节不变**
    ——`tests/test_reliability_hardening.py::test_sqlite_engine_kwargs_unchanged_by_pg_hardening`
    有一条精确相等断言守着它（本地开发/测试行为不能因此改变）。

    三条 PRAGMA 各自解决一个实测问题：
    - `journal_mode=WAL`：读不再被写阻塞（默认 rollback journal 下写会挡读）。
    - `synchronous=NORMAL`：默认 FULL 下**每次 COMMIT 一次 fsync**，单机写入只有 36 req/s。
      WAL + NORMAL 仍是"提交即持久"（进程崩溃不丢），仅断电可能丢最后几个事务。
    - `busy_timeout=5000`：SQLite 默认**拿到锁就立刻报 SQLITE_BUSY**（表现为随机的
      "database is locked"）；等 5s 与本仓其它"快速失败"预算同量级。
    """
    from .. import settings

    if not settings.sqlite_wal():
        return                       # 显式关掉 → 保持库默认（旧行为，可审计的选择）

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=5000")
        finally:
            cur.close()


def _make_engine(target: str) -> Engine:
    engine = create_engine(target, **engine_kwargs(target))
    if target.startswith("sqlite"):
        _apply_sqlite_pragmas(engine)
    return engine


def get_sessionmaker() -> sessionmaker:
    get_engine()                     # 内部持锁；返回时 _SessionLocal 必已就绪
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope(url: str | None = None, *, tenant_id: str | None = None,
                  system: bool = False) -> Iterator[Session]:
    """事务作用域：异常回滚，正常提交，最后关闭。

    传 url 时用临时引擎（测试用内存库，不污染全局）。

    RLS：在 PostgreSQL 上，若上下文有租户（参数显式传，或 `set_tenant_context`
    设过），事务内执行 `set_config('app.tenant_id', <t>, true)`（`true`=事务作用域），
    让行级安全策略按本租户放行。SQLite 无 RLS，跳过（应用层过滤仍然生效）。
    用 `set_config(...)` 绑定参数而非拼接 `SET LOCAL`——杜绝 SQL 注入。

    **`system=True`：系统级跨租户扫描**（升级 sweeper、异步 job 恢复扫描这类后台进程）
    ------------------------------------------------------------------------------
    为什么需要这个开关（**曾经的静默失效**）：RLS 策略是
    `tenant_id = current_setting('app.tenant_id', true)`，未设上下文时右侧为 NULL →
    **零行可见**（失败关闭，这是对的）。但后台扫描**本来就没有租户**，于是它扫到 0 行：
    实测（非 owner/非 superuser 的应用角色，PG 16 + 本仓迁移）——
    `list_due_deliveries` 跨租户扫到 **0**，`sweep_once` 永远 `scanned=0`，
    **告警升级在生产的 PG 上根本不会发生**；而 CI 的 PG 腿连的是 superuser（绕过 RLS），
    所以一直没被发现。

    故这里给出一条**显式、可审计**的系统扫描通道：`system=True` 时置
    `app.system_scan='on'`（同样事务作用域），策略侧多一个 `OR` 分支放行。
    三条纪律：
      - **只给后台/系统级动作用**（请求路径绝不传 `system=True`）——全仓调用点可 grep
        `session_scope(system=True)`，每一处都应能说清"为什么这里没有租户"（后台扫描、
        保留清理、租户数据删除、分区维护、探针里的运维口径计数）；
      - 扫描**只负责找出"哪条要处理"**，逐条处理仍走 `session_scope(tenant_id=...)`，
        写入也照旧带该行的 tenant_id；
      - 不设上下文时**默认仍然是零行**（失败关闭不放松）。
    """
    if url is not None:
        engine = get_engine(url)
        maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        session = maker()
    else:
        session = get_sessionmaker()()
    tenant = tenant_id if tenant_id is not None else _current_tenant.get()
    try:
        is_pg = session.bind is not None and session.bind.dialect.name == "postgresql"
        if system and is_pg:
            # 事务作用域（is_local=true）：事务结束自动失效，不泄漏到连接池的下次借用
            session.execute(text("SELECT set_config('app.system_scan', 'on', true)"))
        if tenant and is_pg:
            session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"),
                {"t": tenant})
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(url: str | None = None) -> None:
    """建表（幂等）。**仅供测试 / SQLite 快速建表**。

    不要在生产或任何真 PostgreSQL 路径上用它——`create_all` 建了表却**不写
    `alembic_version`**，于是库里出现"表都在、版本表为空"的楔子状态；之后任何人跑
    `migrations.upgrade_head()` 都会 `DuplicateTable`（本仓库的 PG 测试腿就踩过这个：
    `init_db()` 先建表、`test_state_store` 再 `upgrade_head` → 14 个 error）。
    真库请走 `migrations.upgrade_head()`（生产启动路径 `service/app.py` 用的就是它）。
    """
    from . import models  # noqa: F401  确保模型已注册到 Base.metadata

    target = url or database_url()
    if not target.startswith("sqlite"):
        _log.warning(
            "init_db() 正在对非 sqlite 库执行 create_all：它不会写 alembic_version，"
            "后续 upgrade_head() 会 DuplicateTable。真库请改用 migrations.upgrade_head()",
            extra={"url_scheme": target.split(":", 1)[0]},
        )
    engine = get_engine(url)
    Base.metadata.create_all(engine)


def reset_engine() -> None:
    """清空缓存的引擎（测试切换数据库用）。"""
    global _engine, _SessionLocal
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _SessionLocal = None
