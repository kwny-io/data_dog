"""写路径 T1 调参：连接池容量（PG）与 SQLite PRAGMA。

为什么单独一份（而不是塞进 `test_reliability_hardening.py`）
----------------------------------------------------------
那份文件守的是"**等待必须有上限**"（建连/拿连接/语句三段超时）。这份守的是另一件事：
"**容量必须够用**"——池太小不是"慢"，是"多出来的并发一点吞吐都换不到，只堆在池门口"。
两者是两件不同的故障模式，混在一起会让人以为调大 `pool_timeout` 能解决排队（不能）。

实测依据（`docs/write_path_batching.md`）：同库同目标 RPS，60 并发 vs 15 并发（= 当时池容量）
→ 吞吐 72 vs 73 req/s 几乎不变，P99 1778 vs 290ms（6.1×）。
"""
from __future__ import annotations

import sqlite3

import pytest

from data_dog.storage import db

_PG_URL = "postgresql+psycopg://u:p@127.0.0.1:5432/db"


# ---------------------------------------------------------------- 连接池容量（PG）

def test_pg_engine_kwargs_carry_pool_capacity():
    """PG URL → 池容量来自 settings，而不是库默认的 5+10=15。"""
    kwargs = db.engine_kwargs(_PG_URL)
    assert kwargs["pool_size"] == 10
    assert kwargs["max_overflow"] == 10
    # 与"三段超时"并存，互不替代
    assert kwargs["pool_timeout"] == 5.0
    assert kwargs["pool_pre_ping"] is True


def test_pool_capacity_follows_settings(monkeypatch: pytest.MonkeyPatch):
    """运维可用 DD_DB_POOL_SIZE / DD_DB_MAX_OVERFLOW 调，不必改代码。"""
    monkeypatch.setenv("DD_DB_POOL_SIZE", "40")
    monkeypatch.setenv("DD_DB_MAX_OVERFLOW", "5")
    kwargs = db.engine_kwargs(_PG_URL)
    assert kwargs["pool_size"] == 40
    assert kwargs["max_overflow"] == 5


def test_pool_size_is_at_least_one(monkeypatch: pytest.MonkeyPatch):
    """`0`/负数不是"无限池"，而是非法值 → 收敛为 1（绝不放行 0 = 永远拿不到连接）。"""
    monkeypatch.setenv("DD_DB_POOL_SIZE", "0")
    monkeypatch.setenv("DD_DB_MAX_OVERFLOW", "-3")
    kwargs = db.engine_kwargs(_PG_URL)
    assert kwargs["pool_size"] == 1
    assert kwargs["max_overflow"] == 0


def test_pg_engine_actually_uses_configured_capacity(monkeypatch: pytest.MonkeyPatch):
    """kwargs 落到真引擎上（`create_engine` 不建连，故这里不需要真库）。"""
    monkeypatch.setenv("DD_DB_POOL_SIZE", "7")
    engine = db._make_engine(_PG_URL)
    try:
        assert engine.pool.size() == 7
    finally:
        engine.dispose()


def test_sqlite_engine_kwargs_has_no_pool_settings():
    """SQLite 分支不受影响：既无 PG 专有参数，也无池参数（本地行为逐字节不变）。"""
    kwargs = db.engine_kwargs("sqlite:///local.db")
    assert kwargs == {"future": True, "connect_args": {"check_same_thread": False}}, kwargs


# ---------------------------------------------------------------- SQLite PRAGMA

def _pragma(url: str, name: str):
    """经项目引擎建一条连接后，用**裸 sqlite3** 读库级/连接级 PRAGMA。"""
    engine = db._make_engine(url)
    try:
        with engine.connect() as conn:
            return conn.exec_driver_sql(f"PRAGMA {name}").scalar()
    finally:
        engine.dispose()


def test_sqlite_gets_wal_and_pragmas(tmp_path):
    url = f"sqlite:///{(tmp_path / 't.db').as_posix()}"
    assert _pragma(url, "journal_mode") == "wal"
    assert _pragma(url, "synchronous") == 1          # NORMAL
    assert _pragma(url, "busy_timeout") == 5000


def test_wal_is_a_persistent_property_of_the_file(tmp_path):
    """WAL 是**库级持久**设置：完全绕开项目代码、用裸 sqlite3 打开也应看到 wal。

    这条防的是"PRAGMA 只在某条连接上生效、换个连接就退回 delete"的假修复。
    """
    path = tmp_path / "t.db"
    _pragma(f"sqlite:///{path.as_posix()}", "journal_mode")   # 触发一次建连
    with sqlite3.connect(str(path)) as raw:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_sqlite_wal_can_be_disabled(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """`DD_SQLITE_WAL=0` → 保持库默认（rollback journal + FULL），这是可审计的选择。"""
    monkeypatch.setenv("DD_SQLITE_WAL", "0")
    url = f"sqlite:///{(tmp_path / 'off.db').as_posix()}"
    assert _pragma(url, "journal_mode") == "delete"
    assert _pragma(url, "synchronous") == 2          # FULL


def test_in_memory_sqlite_does_not_break(tmp_path):
    """内存库不支持 WAL（PRAGMA 返回 memory 而非报错）——不能因此炸掉测试用的临时引擎。"""
    engine = db._make_engine("sqlite://")
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() in ("memory", "wal")
    finally:
        engine.dispose()


def test_writer_does_not_block_reader_in_wal(tmp_path):
    """WAL 的实际收益：一个连接持有写事务时，另一个连接仍**读得到**。

    这正是"写入时读 P99 被拖到 8s"的机制性修复——不是调参玄学。
    """
    url = f"sqlite:///{(tmp_path / 'w.db').as_posix()}"
    engine = db._make_engine(url)
    try:
        with engine.begin() as setup:
            setup.exec_driver_sql("CREATE TABLE t (k INTEGER PRIMARY KEY, v TEXT)")

        writer = engine.connect()
        try:
            writer.exec_driver_sql("BEGIN IMMEDIATE")
            writer.exec_driver_sql("INSERT INTO t (k, v) VALUES (1, 'x')")
            # 写事务**未提交**时，另一个连接读（应立刻返回，而不是 database is locked）
            with engine.connect() as reader:
                n = reader.exec_driver_sql("SELECT COUNT(*) FROM t").scalar()
            assert n == 0
            writer.exec_driver_sql("COMMIT")
        finally:
            writer.close()
    finally:
        engine.dispose()
