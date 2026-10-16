"""A9 转换的**回滚路径**：`--keep-old` 必须真的能退回去。

为什么单独立一条：`keep_old` 此前只有源码、**零测试覆盖**，而它是"出事了能退回去"的唯一
备用路径——默认 `keep_old=False` 会把旧表删掉，那时回退只能靠人工。这条把三件事钉死：

1. `keep_old=True` 后旧表仍在、**还是普通表**（不是分区表）；
2. 行数与内容原封不动（"能退回去"的前提是旧表没被动过）；
3. 旧表上的**规范索引名**已被腾出去（加了 `_old` 后缀）——否则新表抢不到 `pk_<表>` 这种
   schema 级唯一名；同时新表自己的规范名必须就位。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from data_dog.storage import partitioning

_PG_URL = os.environ.get("DD_TEST_PG_URL", "").strip()
_needs_pg = pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 回滚路径测试")


@_needs_pg
def test_keep_old_leaves_a_usable_rollback_table():
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from data_dog.storage.models import day_of

    table = "keepold_probe"
    now = datetime(2026, 12, 1, tzinfo=timezone.utc)
    day_a = day_of(datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp())
    day_b = day_of(datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp())
    eng = create_engine(_PG_URL, future=True)
    with eng.begin() as c:
        for name in (table, f"{table}_old", f"{table}_new"):
            c.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
        # 与生产同形：主键约束名显式给成 pk_<表>（生产是 pk_telemetry_events），
        # 这样才验得到 keep_old 对"规范约束名"的处理。
        c.execute(text(
            f"CREATE TABLE {table} (tenant_id varchar(80) NOT NULL, event_id varchar(80) NOT NULL, "
            "created_day bigint NOT NULL, created_at double precision NOT NULL, "
            f"CONSTRAINT pk_{table} PRIMARY KEY (tenant_id, event_id, created_day))"))
        c.execute(text(f"CREATE INDEX ix_{table}_tenant_created ON {table} (tenant_id, created_at)"))
        c.execute(text(f"INSERT INTO {table} VALUES "
                       f"('t1','e1',{day_a},1.0), ('t1','e2',{day_b},2.0)"))
    try:
        with Session(eng) as s:
            out = partitioning.convert_to_partitioned(
                s, table, months=2, now=now, keep_old=True, confirm=table)
            s.commit()
            assert out["old_table"] == f"{table}_old"
            assert partitioning.is_partitioned(s, table) is True
            assert s.execute(text(f"SELECT count(*) FROM {table}_old")).scalar() == 2
            # 旧表仍是普通表，且数据没被搬走（能退回去的前提）
            assert partitioning.is_partitioned(s, f"{table}_old") is False
            assert s.execute(text(f"SELECT count(*) FROM {table}_old WHERE tenant_id='t1'")
                             ).scalar() == 2
            old_idx = [r[0] for r in s.execute(text(
                "SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": f"{table}_old"})]
            assert f"pk_{table}_old" in old_idx, old_idx
            assert f"ix_{table}_tenant_created_old" in old_idx, old_idx
            # 新表的规范名必须就位（旧表腾出来了）
            new_idx = [r[0] for r in s.execute(text(
                "SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": table})]
            assert f"pk_{table}" in new_idx, new_idx
            assert f"ix_{table}_tenant_created" in new_idx, new_idx
    finally:
        with eng.begin() as c:
            for name in (table, f"{table}_old", f"{table}_new"):
                c.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
        eng.dispose()
