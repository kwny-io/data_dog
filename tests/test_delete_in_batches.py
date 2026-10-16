"""`delete_in_batches` 的**直接**测试——这个函数此前只有 retention/tenant 的间接覆盖。

要守住三件事：
1. 分批真删干净、批数如实（`batches == max_batches` 的含义是"本轮没清完"，不是"清完了"）；
2. 单列主键与复合主键两条路径都走通（单列走 `in_()`，复合走 `tuple_(*pk).in_()`）；
3. `batch_size` 超上限**明确拒绝**，而不是让任务在半夜失败。

为什么上限是个真问题：`batch_size` 从 `retention.py` / `tenant_data.py` 一路透传到 CLI 的
`--batch-size`，原先只校验 `>= 1`。真 PG 16.15 上 10 万行实测：batch=5000 要 138s（比 500
慢 6.5×），batch=20000 直接 `psycopg.errors.StatementTooComplex: stack depth limit exceeded`。
"""
from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, func, select
from sqlalchemy.orm import Session

from data_dog.storage.repository import MAX_DELETE_BATCH, delete_in_batches


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{(tmp_path / 'del.db').as_posix()}")
    yield eng
    eng.dispose()


def _composite(metadata: MetaData) -> Table:
    return Table("del_composite", metadata,
                 Column("tenant_id", String(20), primary_key=True),
                 Column("id", String(20), primary_key=True),
                 Column("created_at", Integer))


def _single(metadata: MetaData) -> Table:
    return Table("del_single", metadata,
                 Column("id", Integer, primary_key=True),
                 Column("created_at", Integer))


def _seed(engine, table: Table, n: int) -> None:
    with engine.begin() as c:
        if len(table.primary_key.columns) == 1:
            c.execute(table.insert(), [{"id": i, "created_at": 100 + i} for i in range(n)])
        else:
            c.execute(table.insert(), [{"tenant_id": "t", "id": str(i), "created_at": 100 + i}
                                       for i in range(n)])


def _count(engine, table: Table) -> int:
    with engine.connect() as c:
        return int(c.execute(select(func.count()).select_from(table)).scalar_one())


def test_composite_pk_deletes_everything_in_batches(engine):
    md = MetaData()
    table = _composite(md)
    md.create_all(engine)
    _seed(engine, table, 10)
    with Session(engine) as s:
        deleted, batches = delete_in_batches(s, table, table.c.created_at >= 0, batch_size=3)
        s.commit()
    assert (deleted, batches) == (10, 4)          # 3+3+3+1
    assert _count(engine, table) == 0


def test_single_column_pk_uses_the_in_path(engine):
    md = MetaData()
    table = _single(md)
    md.create_all(engine)
    _seed(engine, table, 7)
    with Session(engine) as s:
        deleted, batches = delete_in_batches(s, table, table.c.created_at >= 0, batch_size=3)
        s.commit()
    assert (deleted, batches) == (7, 3)           # 3+3+1
    assert _count(engine, table) == 0


def test_max_batches_caps_the_work_and_says_so(engine):
    """批数用满 = 本轮没清完；调用方据此下轮继续，而不是无限期占着库。"""
    md = MetaData()
    table = _composite(md)
    md.create_all(engine)
    _seed(engine, table, 10)
    with Session(engine) as s:
        deleted, batches = delete_in_batches(s, table, table.c.created_at >= 0,
                                             batch_size=3, max_batches=2)
        s.commit()
    assert (deleted, batches) == (6, 2)
    assert _count(engine, table) == 4


def test_condition_is_honoured(engine):
    md = MetaData()
    table = _composite(md)
    md.create_all(engine)
    _seed(engine, table, 10)
    with Session(engine) as s:
        deleted, batches = delete_in_batches(s, table, table.c.created_at < 105, batch_size=2)
        s.commit()
    assert (deleted, batches) == (5, 3)
    assert _count(engine, table) == 5


def test_batch_size_above_the_ceiling_is_refused_before_touching_anything(engine):
    md = MetaData()
    table = _composite(md)
    md.create_all(engine)
    _seed(engine, table, 5)
    with Session(engine) as s:
        with pytest.raises(ValueError, match="超过上限"):
            delete_in_batches(s, table, table.c.created_at >= 0,
                              batch_size=MAX_DELETE_BATCH + 1)
        s.rollback()
    assert _count(engine, table) == 5            # 拒绝发生在删任何一行之前


def test_table_without_primary_key_is_refused(engine):
    md = MetaData()
    table = Table("del_nopk", md, Column("x", Integer))
    md.create_all(engine)
    with Session(engine) as s:
        with pytest.raises(RuntimeError, match="没有主键"):
            delete_in_batches(s, table, table.c.x >= 0, batch_size=10)