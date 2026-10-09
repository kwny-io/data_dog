"""`state/factory.py` 的后端选择测试。

为什么要单独钉住这个文件：两个后端槽位（业务状态 / 限流）都是由**环境变量**
决定的运行时选择，选错的后果是**静默错误**——多副本下限流与候选合并各算各的
（`memory` 后端只在进程内共享）。故这里断言三件事：

1. 按 `DD_DATABASE_URL` 自动选后端（SQLite → memory；PG 串 → postgres）；
2. 显式配置非法时**失败关闭**（拿不准就报错，绝不悄悄退回 memory）；
3. 限流后端可与状态后端**分离**，且各自是缓存单例（`_state` / `_rate`）。

不使用真实 PG/Redis 连接：两个后端的构造函数都是惰性的（`create_engine` /
`Redis.from_url` 都不建连），故本文件完全离线。
"""
from __future__ import annotations

import pytest

from data_dog.state import get_rate_store, get_state_store, reset_state_store
from data_dog.state.memory import InMemoryStateStore
from data_dog.state.pg import PostgresStateStore
from data_dog.state.redis import RedisStateStore

_ENV_KEYS = ("DD_STATE_BACKEND", "DD_RATE_BACKEND", "DD_DATABASE_URL", "DD_REDIS_URL")


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉所有相关环境变量并清空单例，保证用例之间互不串味。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    reset_state_store()


def test_auto_backend_memory_for_sqlite(monkeypatch):
    """未显式配置 + SQLite 串 → memory 后端，且多次调用返回同一实例（单例）。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DD_DATABASE_URL", "sqlite:///unused-for-this-decision.db")

    store = get_state_store()
    assert isinstance(store, InMemoryStateStore)
    assert get_state_store() is store          # 缓存单例：不重复构造
    print("PASS test_auto_backend_memory_for_sqlite")


def test_auto_backend_postgres_for_pg_url(monkeypatch):
    """未显式配置 + PG 连接串 → 自动选 postgres（多副本安全默认）。

    用 `postgresql+psycopg://`：本仓生产驱动是 psycopg3；构造函数只建引擎不建连。
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv("DD_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:5432/dd")

    store = get_state_store()
    assert isinstance(store, PostgresStateStore)
    assert not isinstance(store, InMemoryStateStore)
    print("PASS test_auto_backend_postgres_for_pg_url")


def test_explicit_postgres_rejects_non_pg_url(monkeypatch):
    """显式要 postgres 但给的是 SQLite 串 → RuntimeError（失败关闭，不降级）。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DD_STATE_BACKEND", "postgres")
    monkeypatch.setenv("DD_DATABASE_URL", "sqlite:///not-postgres.db")

    with pytest.raises(RuntimeError, match="需要 PostgreSQL"):
        get_state_store()
    print("PASS test_explicit_postgres_rejects_non_pg_url")


def test_explicit_redis_backend_uses_redis_url(monkeypatch):
    """显式 redis → RedisStateStore；未配限流后端时限流跟随状态后端（同一实例）。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DD_STATE_BACKEND", "redis")
    monkeypatch.setenv("DD_REDIS_URL", "redis://127.0.0.1:6399/7")

    store = get_state_store()
    assert isinstance(store, RedisStateStore)
    # 未配 DD_RATE_BACKEND → 同源（不是新实例），默认不拆两个槽位
    assert get_rate_store() is store
    print("PASS test_explicit_redis_backend_uses_redis_url")


def test_rate_backend_splits_from_state_backend(monkeypatch):
    """限流后端与状态后端可分离：状态走 memory、限流走 redis，各自缓存。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DD_STATE_BACKEND", "memory")
    monkeypatch.setenv("DD_RATE_BACKEND", "redis")
    monkeypatch.setenv("DD_REDIS_URL", "redis://127.0.0.1:6399/8")

    state = get_state_store()
    rate = get_rate_store()
    assert isinstance(state, InMemoryStateStore)
    assert isinstance(rate, RedisStateStore)
    assert rate is not state                   # 真的拆开了
    assert get_rate_store() is rate            # `_rate` 也是缓存单例
    print("PASS test_rate_backend_splits_from_state_backend")


def test_unknown_backend_fails_closed(monkeypatch):
    """未知后端名 → RuntimeError，并列出可用档位（不静默退回 memory）。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DD_STATE_BACKEND", "cassandra")

    with pytest.raises(RuntimeError, match="未知的状态后端"):
        get_state_store()
    print("PASS test_unknown_backend_fails_closed")


def test_reset_state_store_rebuilds_fresh_instances(monkeypatch):
    """`reset_state_store()` 真的清空缓存：清完再取是**新**对象（测试切后端的前提）。"""
    _clear_env(monkeypatch)
    monkeypatch.setenv("DD_STATE_BACKEND", "memory")
    monkeypatch.setenv("DD_RATE_BACKEND", "memory")

    state = get_state_store()
    rate = get_rate_store()
    reset_state_store()
    assert get_state_store() is not state
    assert get_rate_store() is not rate
    print("PASS test_reset_state_store_rebuilds_fresh_instances")
