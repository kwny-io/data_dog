"""分区表的**反向恢复**（`unpartition`）：转换的兜底退路。

为什么单独立一条：`convert_to_partitioned` 默认 `keep_old=False` 会把旧表删掉，此前**没有反向
脚本**——转换提交后想退回普通表只能人工搬。这里把恢复路径的四件事钉死：

1. 往返：普通表 → 分区表 → 普通表，主键回到 `(tenant_id, event_id)`；
2. 数据与普通索引名在往返后仍然正确；
3. **有跨天重复时前置检查会拦住**（旧主键建不起来）——失败关闭，不静默丢行；
4. 确认令牌与转换同规矩（不可逆动作要求复述表名）。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from data_dog.storage import partitioning

_PG_URL = os.environ.get("DD_TEST_PG_URL", "").strip()
_needs_pg = pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 反向恢复测试")

_TABLE = "unpart_probe"
_ALL = (_TABLE, f"{_TABLE}_old", f"{_TABLE}_new", f"{_TABLE}_plain")


def _day(y: int, m: int, d: int) -> int:
    from data_dog.storage.models import day_of

    return day_of(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


def _make_partitioned(eng) -> None:
    """建一张会话里常用的分区表（含普通索引与 RLS）。"""
    from sqlalchemy import text

    with eng.begin() as c:
        for name in _ALL:
            c.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
        c.execute(text(
            f"CREATE TABLE {_TABLE} (tenant_id varchar(80) NOT NULL, event_id varchar(80) NOT NULL, "
            "created_day bigint NOT NULL, created_at double precision NOT NULL, "
            "CONSTRAINT pk_unpart_probe PRIMARY KEY (tenant_id, event_id, created_day)) "
            "PARTITION BY RANGE (created_day)"))
        c.execute(text(f"CREATE TABLE {_TABLE}_202610 PARTITION OF {_TABLE} "
                       f"FOR VALUES FROM ({_day(2026, 10, 1)}) TO ({_day(2026, 11, 1)})"))
        c.execute(text(f"CREATE TABLE {_TABLE}_202611 PARTITION OF {_TABLE} "
                       f"FOR VALUES FROM ({_day(2026, 11, 1)}) TO ({_day(2026, 12, 1)})"))
        c.execute(text(f"CREATE INDEX ix_{_TABLE}_created_at ON {_TABLE} (created_at)"))
        c.execute(text(f"CREATE INDEX ix_{_TABLE}_tenant_created ON {_TABLE} (tenant_id, created_at)"))
        c.execute(text(f"INSERT INTO {_TABLE} VALUES "
                       f"('t1','e1',{_day(2026, 10, 15)},1.0), ('t1','e2',{_day(2026, 11, 15)},2.0)"))
        c.execute(text(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY"))
        c.execute(text(f"CREATE POLICY p_unpart ON {_TABLE} "
                       "USING (tenant_id = current_setting('app.tenant_id', true))"))


def _drop(eng) -> None:
    from sqlalchemy import text

    with eng.begin() as c:
        for name in _ALL + (f"{_TABLE}_202610", f"{_TABLE}_202611"):
            c.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))


@_needs_pg
def test_round_trip_returns_to_a_plain_table():
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    eng = create_engine(_PG_URL, future=True)
    _make_partitioned(eng)
    try:
        with Session(eng) as s:
            assert partitioning.is_partitioned(s, _TABLE) is True
            out = partitioning.unpartition(s, _TABLE, confirm=_TABLE)
            s.commit()
            assert out["unpartitioned"] is True and out["rows"] == 2
            assert partitioning.is_partitioned(s, _TABLE) is False
            pk = s.execute(text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = to_regclass(:t) AND contype = 'p'"),
                {"t": _TABLE}).scalar()
            assert pk == "PRIMARY KEY (tenant_id, event_id)", pk
            assert s.execute(text(f"SELECT count(*) FROM {_TABLE}")).scalar() == 2
            # 普通索引名必须回到规范名（INCLUDING ALL 派生的是 <普通表>_<列>_idx）
            idx = [r[0] for r in s.execute(text(
                "SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": _TABLE})]
            assert f"ix_{_TABLE}_created_at" in idx, idx
            assert f"ix_{_TABLE}_tenant_created" in idx, idx
            # RLS 与策略跟着回来
            rls = s.execute(text(
                "SELECT relrowsecurity FROM pg_class WHERE relname = :t"),
                {"t": _TABLE}).scalar()
            assert rls is True
            assert s.execute(text(
                "SELECT count(*) FROM pg_policy WHERE polrelid = to_regclass(:t)"),
                {"t": _TABLE}).scalar() == 1
            # 原分区表已删（keep_partitioned=False 的快照）
            assert s.execute(text("SELECT to_regclass(:t)"),
                             {"t": f"{_TABLE}_old"}).scalar() is None
    finally:
        _drop(eng)
        eng.dispose()


@_needs_pg
def test_cross_day_duplicates_block_the_reverse():
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    eng = create_engine(_PG_URL, future=True)
    _make_partitioned(eng)
    try:
        with eng.begin() as c:
            # 同一 (tenant, event_id) 两个日桶——旧主键建不起来
            c.execute(text(f"INSERT INTO {_TABLE} VALUES "
                           f"('dup','d1',{_day(2026, 10, 5)},3.0), "
                           f"('dup','d1',{_day(2026, 11, 5)},4.0)"))
        with Session(eng) as s:
            blockers = partitioning.unpartition_blockers(s, _TABLE)
            assert blockers and "跨天重复" in blockers[0], blockers
            with pytest.raises(partitioning.ConversionRefused):
                partitioning.unpartition(s, _TABLE, confirm=_TABLE)
            s.rollback()
        # 失败关闭：表结构没动、数据一行不少
        with Session(eng) as s:
            assert partitioning.is_partitioned(s, _TABLE) is True
            assert s.execute(text(f"SELECT count(*) FROM {_TABLE}")).scalar() == 4
    finally:
        _drop(eng)
        eng.dispose()


@_needs_pg
def test_reverse_requires_the_confirm_token():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    eng = create_engine(_PG_URL, future=True)
    _make_partitioned(eng)
    try:
        with Session(eng) as s:
            with pytest.raises(partitioning.ConversionRefused) as exc:
                partitioning.unpartition(s, _TABLE, confirm="")
            assert "确认令牌" in str(exc.value)
            s.rollback()
    finally:
        _drop(eng)
        eng.dispose()
