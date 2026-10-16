"""`created_day` 分批回填（A9 大表前置）：幂等、可中断、公式与迁移一致。

为什么单独测这个：`docs/storage.md` 的 runbook 写着"大表应分批回填"，但此前**没有工具**，
而迁移里是一条 `UPDATE`（亿级表 = 长事务）。这里把三件事钉住：

1. **公式**：库里的 `_backfill_expr` 必须与迁移里的**逐字一致**（漂移 = 主键与分区键不一致）；
2. **分批 + 幂等**：每批可提交、中断重跑只碰 `created_day IS NULL`；
3. **真库分页正确**：PG 走 `ctid`、SQLite 走 `rowid`，两者都要真的分批（不是一把梭）。
"""
from __future__ import annotations

import importlib.util
import os
import pathlib

import pytest
from sqlalchemy import text

from data_dog.storage import partitioning
from data_dog.storage.db import session_scope
from data_dog.storage.pg_locks import PARTITION_BACKFILL_LOCK_KEY, advisory_session_lock

ROOT = pathlib.Path(__file__).resolve().parents[1]
_PG_URL = os.environ.get("DD_TEST_PG_URL", "").strip()
_needs_pg = pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 回填集成测试")

_DDL = ("CREATE TABLE telemetry_events (tenant_id TEXT NOT NULL, event_id TEXT NOT NULL, "
        "created_at DOUBLE PRECISION, created_day BIGINT, PRIMARY KEY (tenant_id, event_id))")
# (event_id, created_at, 期望的 created_day)：含边界值——86400 整点、以及 .9/.5 的小数部分
_ROWS = [("e1", 0.0, 0), ("e2", 86399.9, 0), ("e3", 86400.0, 1), ("e4", 172800.5, 2)]


def test_library_formula_matches_the_migration():
    """公式漂移的后果很隐蔽：回填写进去的值与迁移建的主键/分区键不一致。"""
    paths = sorted((ROOT / "alembic" / "versions").glob("*day_bucket_pk.py"))
    assert paths, "找不到 day_bucket 迁移"
    spec = importlib.util.spec_from_file_location("_dd_day_bucket_probe", paths[0])
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for dialect in ("postgresql", "sqlite"):
        assert mod._backfill_expr(dialect) == partitioning._backfill_expr(dialect), (
            f"{dialect} 的 floor(created_at/86400) 写法在迁移与库里不一致")


def _sqlite_session():
    return session_scope(url="sqlite+pysqlite:///:memory:", system=True)


def _seed(session) -> None:
    session.execute(text(_DDL))
    for event_id, created_at, _ in _ROWS:
        session.execute(text("INSERT INTO telemetry_events (tenant_id, event_id, created_at) "
                             "VALUES (:t, :e, :c)"), {"t": "t1", "e": event_id, "c": created_at})
    session.commit()


def _values(session) -> dict[str, int | None]:
    rows = session.execute(text("SELECT event_id, created_day FROM telemetry_events")).all()
    return {str(r[0]): (None if r[1] is None else int(r[1])) for r in rows}


def test_batched_backfill_fills_every_row_with_the_exact_day():
    with _sqlite_session() as s:
        _seed(s)
        rep = partitioning.backfill_created_day(s, batch=2)
        assert rep["updated"] == len(_ROWS)
        assert rep["batches"] >= 3, "batch=2 处理 4 行至少要 3 批（最后一批 0 行收尾）"
        assert rep["remaining"] == 0
        assert _values(s) == {e: day for e, _, day in _ROWS}


def test_second_run_is_a_noop_and_partially_filled_rows_are_left_alone():
    """幂等：重跑不重复写；已填过的行**不被覆盖**（可能被人手工修正过）。"""
    with _sqlite_session() as s:
        _seed(s)
        s.execute(text("UPDATE telemetry_events SET created_day = 99 WHERE event_id = :e"),
                  {"e": "e1"})
        s.commit()
        rep = partitioning.backfill_created_day(s, batch=2)
        assert rep["updated"] == len(_ROWS) - 1
        assert _values(s)["e1"] == 99, "已填过的行不该被覆盖"
        again = partitioning.backfill_created_day(s, batch=2)
        assert (again["updated"], again["batches"]) == (0, 1), "第二次应是 no-op"


def test_commit_each_false_keeps_the_caller_in_charge_of_the_transaction():
    """`commit_each=False` 时库函数不偷偷提交：调用方仍能整体回滚。"""
    with _sqlite_session() as s:
        _seed(s)
        rep = partitioning.backfill_created_day(s, batch=2, commit_each=False)
        assert rep["updated"] == len(_ROWS)
        s.rollback()
        assert set(_values(s).values()) == {None}, "回滚后应回到「全都没填」的状态"


@_needs_pg
def test_pg_batched_backfill_uses_ctid_and_is_idempotent():
    """真库：PG 走 `ctid` 分页，批量与幂等都要成立。"""
    with session_scope(url=_PG_URL, system=True) as s:
        s.execute(text("DROP TABLE IF EXISTS backfill_probe"))
        s.execute(text("CREATE TABLE backfill_probe (tenant_id TEXT NOT NULL, event_id TEXT NOT NULL, "
                       "created_at DOUBLE PRECISION, created_day BIGINT, "
                       "PRIMARY KEY (tenant_id, event_id))"))
        for event_id, created_at, _ in _ROWS:
            s.execute(text("INSERT INTO backfill_probe (tenant_id, event_id, created_at) "
                           "VALUES (:t, :e, :c)"), {"t": "t1", "e": event_id, "c": created_at})
        s.commit()
        rep = partitioning.backfill_created_day(s, "backfill_probe", batch=2)
        assert rep["updated"] == len(_ROWS) and rep["batches"] >= 3
        got = dict(s.execute(text("SELECT event_id, created_day FROM backfill_probe")).all())
        assert {str(k): int(v) for k, v in got.items()} == {e: day for e, _, day in _ROWS}
        assert partitioning.backfill_created_day(s, "backfill_probe", batch=2)["updated"] == 0
        s.execute(text("DROP TABLE backfill_probe"))
        s.commit()


@_needs_pg
def test_pg_session_lock_excludes_a_second_backfill():
    """两个运维同时跑：第二个必须抢不到（而不是并行白干 / 互相拖慢）。"""
    with session_scope(url=_PG_URL, system=True) as a:
        with advisory_session_lock(a, PARTITION_BACKFILL_LOCK_KEY, wait=False) as got:
            assert got is True
            with session_scope(url=_PG_URL, system=True) as b:
                with advisory_session_lock(b, PARTITION_BACKFILL_LOCK_KEY, wait=False) as other:
                    assert other is False, "第二个会话不该拿到回填锁"
        with session_scope(url=_PG_URL, system=True) as c:
            with advisory_session_lock(c, PARTITION_BACKFILL_LOCK_KEY, wait=False) as after:
                assert after is True, "第一个退出后应能拿到"
