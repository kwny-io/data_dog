"""`storage/pg_locks.py`：不依赖任何表的**库层硬互斥**（迁移入口 + 分区转换事务）。

分两类：
- **离线**（默认腿，SQLite）：非 PG 是 **no-op**——加锁不能把单机部署变成"必须 PG"；
- **真库**（设了 `DD_TEST_PG_URL` 才跑）：互斥语义本身（第二个连接拿不到）+ 释放后能拿到。
"""
from __future__ import annotations

import os
import time

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


def test_try_xact_lock_is_non_blocking_and_noop_on_sqlite():
    """只读探测原语：SQLite no-op 返回 True；且不执行 PG 专有 SQL。"""
    with session_scope(url="sqlite+pysqlite:///:memory:", system=True) as s:
        assert pg_locks.try_xact_lock(s, pg_locks.PARTITION_CONVERT_LOCK_KEY) is True


@_needs_pg
def test_try_xact_lock_returns_false_while_another_session_holds_it():
    """真库：A 持有未提交时，B 用 try 立刻得 False（**不阻塞**）；A 提交后得 True。"""
    key = pg_locks.PARTITION_CONVERT_LOCK_KEY
    with session_scope(url=_PG_URL, system=True) as a:
        assert pg_locks.acquire_xact_lock(a, key) is True
        with session_scope(url=_PG_URL, system=True) as b:
            assert pg_locks.try_xact_lock(b, key) is False, "第二个会话不该拿到"
            b.rollback()
        a.commit()
        with session_scope(url=_PG_URL, system=True) as c:
            assert pg_locks.try_xact_lock(c, key) is True, "提交后应已释放"
            c.rollback()


# ---------------------------------------------------------------- 有界等待（离线）
# 背景（2026-10-25 实测）：`migration_lock` 原来用阻塞式 `pg_advisory_lock`。在 compose 里
# 起服务（旧库卷 + 残留会话持锁）时，alembic 打印完 `Running upgrade -> …` 就再无下文：
# 无 ERROR、无超时、不退出，而 `/healthz` 仍 200 → "服务起来了但永远不 ready"，
# 现场只看到其他告警刷屏（同时 worker 无退避地打了 5919 条 WARN，把真相淹没）。
# 下面三条不依赖真库：用假引擎证明"锁拿不到时**会自己放弃**并说清怎么排查"。


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConn:
    """记录执行过的 SQL；`grant_after` 次尝试后开始返回 True（模拟别人释放锁）。"""

    def __init__(self, root):
        self._root = root

    def execute(self, stmt, params=None):        # noqa: ARG002 - 断言里看 SQL
        sql = str(stmt)
        self._root.sql.append(sql)
        if "try_advisory_lock" in sql:
            self._root.tries += 1
            return _FakeResult(self._root.tries > self._root.grant_after)
        return _FakeResult(None)

    def commit(self):
        self._root.commits += 1

    def close(self):
        self._root.closed = True


class _FakeEngine:
    def __init__(self, root):
        self._root = root
        self.dialect = type("D", (), {"name": "postgresql"})()

    def connect(self):
        return _FakeConn(self._root)

    def dispose(self):
        self._root.disposed = True


def _fake_engine(monkeypatch, *, grant_after=10**9):
    root = type("R", (), {})()
    root.sql, root.tries, root.commits = [], 0, 0
    root.closed = root.disposed = False
    root.grant_after = grant_after
    monkeypatch.setattr(pg_locks, "create_engine", lambda url: _FakeEngine(root))
    return root


def test_migration_lock_gives_up_after_timeout_instead_of_hanging(monkeypatch):
    """锁一直拿不到 → 在预算内抛 `MigrationLockTimeout`，而不是无声等下去。"""
    monkeypatch.setenv("DD_MIGRATION_LOCK_TIMEOUT", "0.05")
    root = _fake_engine(monkeypatch)
    start = time.monotonic()
    with pytest.raises(pg_locks.MigrationLockTimeout) as ei:
        with pg_locks.migration_lock("postgresql+psycopg://x/y"):
            pytest.fail("不该进入临界区")
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, "必须有上界（不能无限阻塞）"
    msg = str(ei.value)
    # 错误信息要能直接指导排查（否则等于只留一句"超时"）
    assert "pg_locks" in msg and "pg_terminate_backend" in msg and "advisory" in msg
    assert root.closed is True, "超时也要关掉那条连接（不能泄漏）"


def test_migration_lock_retries_then_proceeds_and_unlocks(monkeypatch):
    """别人短暂持锁 → 重试后拿到锁并正常进临界区，退出时解锁。"""
    monkeypatch.setenv("DD_MIGRATION_LOCK_TIMEOUT", "5")
    root = _fake_engine(monkeypatch, grant_after=2)
    with pg_locks.migration_lock("postgresql+psycopg://x/y") as held:
        assert held is True
    assert root.tries >= 3, "应该真的重试过（而不是阻塞等）"
    assert any("pg_advisory_unlock" in s for s in root.sql), "退出必须解锁"
    assert root.closed is True


def test_migration_lock_never_uses_blocking_lock(monkeypatch):
    """**回归守卫**：不能再退回阻塞式 `pg_advisory_lock`（无 `try_` 前缀的那个）。"""
    monkeypatch.setenv("DD_MIGRATION_LOCK_TIMEOUT", "5")
    root = _fake_engine(monkeypatch, grant_after=0)
    with pg_locks.migration_lock("postgresql+psycopg://x/y"):
        pass
    blocking = [s for s in root.sql
                if "pg_advisory_lock" in s and "try_advisory_lock" not in s]
    assert not blocking, f"出现阻塞式取锁：{blocking}"


def test_lock_timeout_is_configurable(monkeypatch):
    """超时可配（默认 60s）：`DD_MIGRATION_LOCK_TIMEOUT` 生效且不为负。"""
    from data_dog import settings

    monkeypatch.delenv("DD_MIGRATION_LOCK_TIMEOUT", raising=False)
    assert settings.migration_lock_timeout_s() == 60.0
    monkeypatch.setenv("DD_MIGRATION_LOCK_TIMEOUT", "12")
    assert settings.migration_lock_timeout_s() == 12.0
    monkeypatch.setenv("DD_MIGRATION_LOCK_TIMEOUT", "-5")
    assert settings.migration_lock_timeout_s() == 0.0
