"""数据库引擎 / 会话管理（SQLAlchemy 2.0）。

- 默认 SQLite（本地零安装）；设 `DD_DATABASE_URL` 切 PostgreSQL，代码不改。
- SQLite 需要 `check_same_thread=False`（多线程/测试）；PostgreSQL 用连接池。
- 提供上下文管理器 `session_scope()`：自动 commit / rollback / close。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..paths import data_dir


def _default_sqlite_url() -> str:
    """本地 SQLite 默认位置（运行时解析，可用 DD_DATA_DIR 覆盖）。"""
    return f"sqlite:///{(data_dir() / 'data_dog.db').as_posix()}"

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


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
    if _engine is None or url is not None:
        kwargs: dict[str, Any] = {"future": True}
        if target.startswith("sqlite"):
            # SQLite：允许跨线程 + 不要求严格类型
            kwargs["connect_args"] = {"check_same_thread": False}
            engine = create_engine(target, **kwargs)
        else:
            # PostgreSQL 等：连接池预检，防陈旧连接
            kwargs["pool_pre_ping"] = True
            engine = create_engine(target, **kwargs)
        if url is not None:
            return engine                      # 临时引擎（测试用，不缓存）
        _engine = engine
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_sessionmaker() -> sessionmaker:
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    """事务作用域：异常回滚，正常提交，最后关闭。

    传 url 时用临时引擎（测试用内存库，不污染全局）。
    """
    if url is not None:
        engine = get_engine(url)
        maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        session = maker()
    else:
        session = get_sessionmaker()()
    try:
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
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
