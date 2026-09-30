"""数据库引擎 / 会话管理（SQLAlchemy 2.0）。

- 默认 SQLite（本地零安装）；设 `DD_DATABASE_URL` 切 PostgreSQL，代码不改。
- SQLite 需要 `check_same_thread=False`（多线程/测试）；PostgreSQL 用连接池。
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


def _make_engine(target: str) -> Engine:
    kwargs: dict[str, Any] = {"future": True}
    if target.startswith("sqlite"):
        # SQLite：允许跨线程 + 不要求严格类型
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # PostgreSQL 等：连接池预检，防陈旧连接
        kwargs["pool_pre_ping"] = True
    return create_engine(target, **kwargs)


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
