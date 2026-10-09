"""数据库引擎 / 会话管理（SQLAlchemy 2.0）。

- 默认 SQLite（本地零安装）；设 `DD_DATABASE_URL` 切 PostgreSQL，代码不改。
- SQLite 需要 `check_same_thread=False`（多线程/测试）；PostgreSQL 用连接池。
- PostgreSQL 的**建连 / 拿连接 / 单条语句**三段等待都有显式上限（见 `engine_kwargs`）：
  数据库不可用或过载时必须快速失败并报错，而不是静默把请求线程钉住几十秒。
- 提供上下文管理器 `session_scope()`：自动 commit / rollback / close。
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..paths import data_dir

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
    （测试与本机开发都依赖该行为不变）。
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


def _make_engine(target: str) -> Engine:
    return create_engine(target, **engine_kwargs(target))


def get_sessionmaker() -> sessionmaker:
    get_engine()                     # 内部持锁；返回时 _SessionLocal 必已就绪
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope(url: str | None = None, *, tenant_id: str | None = None
                  ) -> Iterator[Session]:
    """事务作用域：异常回滚，正常提交，最后关闭。

    传 url 时用临时引擎（测试用内存库，不污染全局）。

    RLS：在 PostgreSQL 上，若上下文有租户（参数显式传，或 `set_tenant_context`
    设过），事务内执行 `set_config('app.tenant_id', <t>, true)`（`true`=事务作用域），
    让行级安全策略按本租户放行。SQLite 无 RLS，跳过（应用层过滤仍然生效）。
    用 `set_config(...)` 绑定参数而非拼接 `SET LOCAL`——杜绝 SQL 注入。
    """
    if url is not None:
        engine = get_engine(url)
        maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        session = maker()
    else:
        session = get_sessionmaker()()
    tenant = tenant_id if tenant_id is not None else _current_tenant.get()
    try:
        if tenant and session.bind is not None \
                and session.bind.dialect.name == "postgresql":
            # 事务作用域（is_local=true）：事务结束自动失效，不泄漏到连接池的下次借用
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
    """建表（幂等）。应用启动或测试前调用。"""
    from . import models  # noqa: F401  确保模型已注册到 Base.metadata
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
