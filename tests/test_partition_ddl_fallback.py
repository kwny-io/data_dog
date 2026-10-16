"""ensure/drop 的**库层兜底锁**（--no-lease 时）：只在租约被显式关掉时才动 PG 咨询锁。"""
from __future__ import annotations

import os

import pytest

from data_dog.storage import partitioning
from data_dog.storage.pg_locks import PARTITION_ENSURE_LOCK_KEY

_PG_URL = os.environ.get("DD_TEST_PG_URL", "").strip()
_needs_pg = pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 兜底锁集成测试")


def test_lease_off_is_off_by_default(monkeypatch):
    """默认（协作式租约还在管事）不碰咨询锁：默认行为必须逐字不变。"""
    called: list[int] = []
    monkeypatch.setattr(partitioning, "try_xact_lock",
                        lambda _s, k: called.append(k) or True)
    monkeypatch.setattr(partitioning, "is_postgres", lambda _s: True)
    monkeypatch.setattr(partitioning, "is_partitioned", lambda _s, _t=partitioning.PARTITION_TABLE: True)
    monkeypatch.setattr(partitioning, "planned_partition_months", lambda *_a, **_k: [])
    assert partitioning.ensure_partitions(object()) == []
    assert called == [], "租约没关时不该去抢库层兜底锁"


def test_lease_off_raises_busy_when_the_fallback_lock_is_held(monkeypatch):
    """--no-lease 且兜底锁被占 → 抛 PartitioningBusy（失败关闭、不排队），且一个 DDL 都没执行。"""
    executed: list[str] = []
    monkeypatch.setattr(partitioning, "is_postgres", lambda _s: True)
    monkeypatch.setattr(partitioning, "is_partitioned", lambda _s, _t=partitioning.PARTITION_TABLE: True)
    monkeypatch.setattr(partitioning, "try_xact_lock", lambda _s, _k: False)
    monkeypatch.setattr(partitioning, "planned_partition_months",
                        lambda *_a, **_k: executed.append("plan") or [])
    with pytest.raises(partitioning.PartitioningBusy) as e:
        partitioning.ensure_partitions(object(), lease_off=True)
    assert "ddpe" in str(e.value)
    assert executed == [], "没抢到兜底锁却还是算了计划/动 DDL"


def test_lease_off_takes_the_lock_when_free(monkeypatch):
    """兜底锁空闲 → 正常执行（走的是同一个 ensure 路径）。"""
    monkeypatch.setattr(partitioning, "is_postgres", lambda _s: True)
    monkeypatch.setattr(partitioning, "is_partitioned", lambda _s, _t=partitioning.PARTITION_TABLE: True)
    seen: list[int] = []
    monkeypatch.setattr(partitioning, "try_xact_lock", lambda _s, k: seen.append(k) or True)
    monkeypatch.setattr(partitioning, "planned_partition_months", lambda *_a, **_k: [])
    assert partitioning.ensure_partitions(object(), lease_off=True) == []
    assert seen == [PARTITION_ENSURE_LOCK_KEY]


def test_ensure_and_drop_use_their_own_key_not_the_convert_one(monkeypatch):
    """不同钥匙是刻意的：转换事务已持有 convert 锁，同事务再 try 会得 False（自己挡自己）。"""
    from data_dog.storage.pg_locks import PARTITION_CONVERT_LOCK_KEY

    assert PARTITION_ENSURE_LOCK_KEY != PARTITION_CONVERT_LOCK_KEY


@_needs_pg
def test_fallback_lock_excludes_a_second_session_on_real_pg():
    """真 PG：A 持有兜底锁时 B 拿不到（真并发下 IF NOT EXISTS 会争 pg_class）。"""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from data_dog.storage import pg_locks

    eng = create_engine(_PG_URL, future=True)
    try:
        with Session(eng) as a:
            assert pg_locks.acquire_xact_lock(a, PARTITION_ENSURE_LOCK_KEY) is True
            with Session(eng) as b:
                assert pg_locks.try_xact_lock(b, PARTITION_ENSURE_LOCK_KEY) is False
                b.rollback()
            a.commit()
        with Session(eng) as c:
            assert pg_locks.try_xact_lock(c, PARTITION_ENSURE_LOCK_KEY) is True
            c.rollback()
    finally:
        with eng.begin() as c:
            c.execute(text("SELECT 1"))
        eng.dispose()
