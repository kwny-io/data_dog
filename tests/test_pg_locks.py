"""`storage/pg_locks.py`：不依赖任何表的**库层硬互斥**（迁移入口 + 分区转换事务）。

分两类：
- **离线**（默认腿，SQLite）：非 PG 是 **no-op**——加锁不能把单机部署变成"必须 PG"；
- **真库**（设了 `DD_TEST_PG_URL` 才跑）：互斥语义本身（第二个连接拿不到）+ 释放后能拿到。
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from data_dog.storage import pg_locks
from data_dog.storage.db import session_scope

_PG_URL = os.environ.get("DD_TEST_PG_URL", "").strip()
_needs_pg = pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 咨询锁集成测试")


def test_keys_are_distinct_constants():
    """两个锁不能共用一把钥匙，否则"迁移"会和"分区转换"互相挡。"""
    assert pg_locks.MIGRATION_LOCK_KEY != pg_locks.PARTITION_CONVERT_LOCK_KEY


def test_non_postgres_is_noop():
    """SQLite：锁全部 no-op，且**不执行任何 PG 专有 SQL**（否则这里会直接报错）。"""
    with session_scope(url="sqlite+pysqlite:///:memory:", system=True) as s:
        assert pg_locks.is_postgres(s) is False
        assert pg_locks.acquire_xact_lock(s, pg_locks.MIGRATION_LOCK_KEY) is True
        assert pg_locks.acquire_xact_lock(s, pg_locks.MIGRATION_LOCK_KEY) is True   # 重复取不报错
        with pg_locks.advisory_xact_lock(s, pg_locks.PARTITION_CONVERT_LOCK_KEY) as got:
            assert got is True
        with pg_locks.advisory_xact_lock(s, pg_locks.MIGRATION_LOCK_KEY, wait=False) as got:
            assert got is True


def test_migration_lock_is_noop_without_postgres(tmp_path):
    """`migration_lock(url)` 在非 PG 上不能连不上就炸——直接 no-op 并如实说"没有互斥"。"""
    url = "sqlite+pysqlite:///" + str(tmp_path / "x.db").replace("\\", "/")
    with pg_locks.migration_lock(url) as held:
        assert held is False


@_needs_pg
def test_xact_lock_excludes_a_second_session():
    """事务级：A 持有未提交时 B 拿不到；A 提交后（自动释放）B 拿得到。"""
    key = pg_locks.PARTITION_CONVERT_LOCK_KEY
    with session_scope(url=_PG_URL, system=True) as a:
        assert pg_locks.acquire_xact_lock(a, key) is True
        with session_scope(url=_PG_URL, system=True) as b:
            with pg_locks.advisory_xact_lock(b, key, wait=False) as got:
                assert got is False, "第二个会话不该拿到已被持有的锁"
        a.commit()
        with session_scope(url=_PG_URL, system=True) as c:
            with pg_locks.advisory_xact_lock(c, key, wait=False) as got:
                assert got is True, "提交后事务级锁应已自动释放"


@_needs_pg
def test_migration_session_lock_excludes_and_releases():
    """会话级（迁移用）：独立连接持有时别人拿不到；出 `with` 后能拿到。"""
    key = pg_locks.MIGRATION_LOCK_KEY
    with pg_locks.migration_lock(_PG_URL) as held:
        assert held is True
        with session_scope(url=_PG_URL, system=True) as other:
            got = bool(other.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                      {"k": key}).scalar())
            assert got is False, "迁移锁应挡住第二个连接"
            other.rollback()
    with session_scope(url=_PG_URL, system=True) as after:
        got = bool(after.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar())
        assert got is True, "迁移结束后锁应已释放"
        after.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})   # 清理池化连接状态
        after.commit()
