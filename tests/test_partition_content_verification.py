"""A9 转换的**内容校验**：行数相等不等于内容相等（整表指纹）。

为什么单独立一个文件：这一层是"证明拷过去的是同一份数据"的机制，不是分区 DDL 本身。
口径与边界写在 `partitioning.content_fingerprint` 的 docstring 里，这里只钉行为：

- 同一库的两张表：一致 → 无差异；截断 / 丢行 → 检出；
- 指纹与**行序无关**（多集和式摘要）：同样的行按不同顺序写入，指纹相同；
- 真 PG：**等长改写**也检出（逐行 md5）；`convert_to_partitioned` 默认 `verified=True`，
  `verify=False` 时只留行数核对且结果里 `verified=False`。

**SQLite 的能力边界（如实钉住，不假装）**：SQLite 构建没有 md5/sha256（实测 3.45.3），
那边的指纹退化成"逐行拼接串长度之和"，所以**等长改写检不出**。下面的
`test_equal_length_rewrite_is_invisible_on_sqlite` 就是这个边界的证据——它断言的是
"检不出"，不是"检得出"；同一条改写在真 PG 上必须被检出。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from data_dog.storage import partitioning

_PG_URL = os.environ.get("DD_TEST_PG_URL", "").strip()
_NOW = datetime(2026, 12, 1, tzinfo=timezone.utc)

_DDL = (
    "CREATE TABLE {name} ("
    " tenant_id varchar(80) NOT NULL,"
    " event_id varchar(80) NOT NULL,"
    " payload text,"
    " note text,"
    " created_at double precision NOT NULL,"
    " created_day bigint)"
)

# 故意含一行全 NULL 的柔性列 + 一行 created_day 为 NULL（转换前的真实形状）。
_ROWS = [
    ("a", "e1", "hello", "n1", 1000000.5, 11),
    ("a", "e2", "world", "n2", 1000001.25, 11),
    ("b", "e1", None, None, 9000000.0, 104),
    ("c", "e3", "x", "y", 1000000.5, None),
]


def _seed(engine, name, rows=None):
    with engine.begin() as cc:
        cc.execute(text("DROP TABLE IF EXISTS " + name))
        cc.execute(text(_DDL.format(name=name)))
        for r in (rows if rows is not None else _ROWS):
            cc.execute(text(
                "INSERT INTO " + name
                + " (tenant_id, event_id, payload, note, created_at, created_day)"
                + " VALUES (:a, :b, :c, :d, :e, :f)"),
                {"a": r[0], "b": r[1], "c": r[2], "d": r[3], "e": r[4], "f": r[5]})


def _fp(engine, name):
    with Session(engine) as s:
        return partitioning.content_fingerprint(s, name)


@pytest.fixture()
def sqlite_engine(tmp_path):
    eng = create_engine(f"sqlite:///{(tmp_path / 'fp.db').as_posix()}", future=True)
    yield eng
    eng.dispose()


# ---------------------------------------------------------------- 离线（SQLite）

def test_identical_tables_have_no_difference(sqlite_engine):
    _seed(sqlite_engine, "fp_a")
    _seed(sqlite_engine, "fp_b")
    fa = _fp(sqlite_engine, "fp_a")
    assert set(fa) == {"n", "len_sum", "digest", "columns"}
    assert fa["n"] == 4
    assert partitioning.fingerprint_diff(fa, _fp(sqlite_engine, "fp_b")) == []


def test_fingerprint_is_order_independent(sqlite_engine):
    """同样的行按**相反顺序**写入，指纹必须相同（和式摘要 + 行内不排序）。"""
    _seed(sqlite_engine, "fp_a")
    _seed(sqlite_engine, "fp_b", rows=list(reversed(_ROWS)))
    fa = _fp(sqlite_engine, "fp_a")
    fb = _fp(sqlite_engine, "fp_b")
    assert fa["n"] == fb["n"]
    assert fa["digest"] == fb["digest"]
    assert partitioning.fingerprint_diff(fa, fb) == []


def test_truncation_is_detected(sqlite_engine):
    _seed(sqlite_engine, "fp_a")
    _seed(sqlite_engine, "fp_b")
    fa = _fp(sqlite_engine, "fp_a")
    with sqlite_engine.begin() as cc:
        cc.execute(text("UPDATE fp_b SET payload = 'hell' WHERE tenant_id = 'a' AND event_id = 'e1'"))
    diff = partitioning.fingerprint_diff(fa, _fp(sqlite_engine, "fp_b"))
    assert any("内容长度不一致" in d for d in diff), diff


def test_row_loss_is_detected(sqlite_engine):
    _seed(sqlite_engine, "fp_a")
    _seed(sqlite_engine, "fp_b")
    fa = _fp(sqlite_engine, "fp_a")
    with sqlite_engine.begin() as cc:
        cc.execute(text("DELETE FROM fp_b WHERE event_id = 'e2'"))
    fb = _fp(sqlite_engine, "fp_b")
    assert fb["n"] == 3
    diff = partitioning.fingerprint_diff(fa, fb)
    assert any("行数不一致" in d for d in diff), diff


def test_null_marker_distinguishes_null_from_empty_string(sqlite_engine):
    """NULL 与空串：SQLite 的 len_sum 都是 0，但摘要里 NULL 编成 ~、空串编成 S，长度仍相同——
    所以这条**也**只有真 PG 能稳稳检出。这里钉住 SQLite 的真实行为，避免过度承诺。"""
    _seed(sqlite_engine, "fp_a")
    _seed(sqlite_engine, "fp_b")
    with sqlite_engine.begin() as cc:
        cc.execute(text("UPDATE fp_b SET payload = '' WHERE tenant_id = 'b' AND event_id = 'e1'"))
    fa = _fp(sqlite_engine, "fp_a")
    fb = _fp(sqlite_engine, "fp_b")
    assert fa["len_sum"] == fb["len_sum"]          # NULL 与 '' 长度都是 0
    assert fa["digest"] == fb["digest"]            # 长度型和式摘要看不出差别
    assert partitioning.fingerprint_diff(fa, fb) == []


def test_equal_length_rewrite_is_invisible_on_sqlite(sqlite_engine):
    """能力边界：等长改写（hello → HELLO）在 SQLite 上**检不出**，因为这里没有 md5。
    这不是缺陷掩盖，而是把"哪边强"写成可执行断言——真 PG 腿上有对称用例要求检出。
    """
    _seed(sqlite_engine, "fp_a")
    _seed(sqlite_engine, "fp_b")
    fa = _fp(sqlite_engine, "fp_a")
    with sqlite_engine.begin() as cc:
        cc.execute(text("UPDATE fp_b SET payload = 'HELLO' WHERE tenant_id = 'a' AND event_id = 'e1'"))
    fb = _fp(sqlite_engine, "fp_b")
    assert fa["len_sum"] == fb["len_sum"]
    assert partitioning.fingerprint_diff(fa, fb) == [], "SQLite 上等长改写检不出是已知边界"


def test_missing_table_is_a_capability_error(sqlite_engine):
    with Session(sqlite_engine) as s:
        with pytest.raises(partitioning.PartitioningUnsupported):
            partitioning.content_fingerprint(s, "no_such_table")


def test_verify_copy_reports_row_loss_without_a_fingerprint(sqlite_engine):
    """`before_fp=None`（verify=False 的路径）仍然必须做**行数**核对。"""
    _seed(sqlite_engine, "fp_a")
    _seed(sqlite_engine, "fp_b")
    with sqlite_engine.begin() as cc:
        cc.execute(text("DELETE FROM fp_b WHERE event_id = 'e2'"))
    with Session(sqlite_engine) as s:
        diff, rows = partitioning._verify_copy(s, "fp_a", "fp_b", None)
    assert rows == 3
    assert any("行数不一致" in d for d in diff), diff


# ---------------------------------------------------------------- 真 PG

@pytest.mark.postgres
@pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 内容校验集成测试")
def test_pg_detects_an_equal_length_rewrite():
    """真 PG 有 md5：同一条等长改写必须被检出（与 SQLite 边界用例对称）。"""
    eng = create_engine(_PG_URL, future=True)
    try:
        _seed(eng, "cv_a")
        _seed(eng, "cv_b")
        base = _fp(eng, "cv_a")
        assert partitioning.fingerprint_diff(base, _fp(eng, "cv_b")) == []
        with eng.begin() as cc:
            cc.execute(text("UPDATE cv_b SET payload = 'HELLO' WHERE tenant_id = 'a' AND event_id = 'e1'"))
        diff = partitioning.fingerprint_diff(base, _fp(eng, "cv_b"))
        assert len(diff) == 1 and "内容摘要不一致" in diff[0], diff
    finally:
        with eng.begin() as cc:
            for name in ("cv_a", "cv_b"):
                cc.execute(text("DROP TABLE IF EXISTS " + name))
        eng.dispose()


@pytest.mark.postgres
@pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 内容校验集成测试")
def test_pg_detects_null_versus_empty_string():
    """真 PG 上 NULL 与空串必须被区分（SQLite 上区分不了，见上面的边界用例）。"""
    eng = create_engine(_PG_URL, future=True)
    try:
        _seed(eng, "cv_a")
        _seed(eng, "cv_b")
        base = _fp(eng, "cv_a")
        with eng.begin() as cc:
            cc.execute(text("UPDATE cv_b SET payload = '' WHERE tenant_id = 'b' AND event_id = 'e1'"))
        diff = partitioning.fingerprint_diff(base, _fp(eng, "cv_b"))
        assert any("内容摘要不一致" in d for d in diff), diff
    finally:
        with eng.begin() as cc:
            for name in ("cv_a", "cv_b"):
                cc.execute(text("DROP TABLE IF EXISTS " + name))
        eng.dispose()


@pytest.mark.postgres
@pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 内容校验集成测试")
def test_verify_copy_catches_a_corrupted_target_on_pg():
    eng = create_engine(_PG_URL, future=True)
    try:
        _seed(eng, "cv_a")
        _seed(eng, "cv_b")
        with Session(eng) as s:
            before = partitioning.content_fingerprint(s, "cv_a")
            s.execute(text("DELETE FROM cv_b WHERE event_id = 'e2'"))
            diff, rows = partitioning._verify_copy(s, "cv_a", "cv_b", before)
        assert rows == 3
        assert any("行数不一致" in d for d in diff), diff
    finally:
        with eng.begin() as cc:
            for name in ("cv_a", "cv_b"):
                cc.execute(text("DROP TABLE IF EXISTS " + name))
        eng.dispose()


@pytest.mark.postgres
@pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 内容校验集成测试")
def test_convert_defaults_to_verified_and_can_opt_out():
    """转换默认采指纹（verified=True）；`verify=False` 只留行数且如实报 verified=False。"""
    eng = create_engine(_PG_URL, future=True)
    table = "cv_conv"
    try:
        with eng.begin() as cc:
            for name in (table, table + "_old", table + "_new"):
                cc.execute(text("DROP TABLE IF EXISTS " + name + " CASCADE"))
            cc.execute(text(
                "CREATE TABLE " + table + " (tenant_id varchar(80) NOT NULL,"
                " event_id varchar(80) NOT NULL, created_day bigint NOT NULL,"
                " created_at double precision NOT NULL, payload text,"
                " CONSTRAINT pk_" + table + " PRIMARY KEY (tenant_id, event_id, created_day))"))
            cc.execute(text("INSERT INTO " + table
                            + " VALUES ('t1','e1',11,1.0,'p1'), ('t1','e2',11,1.0,NULL)"))
        with Session(eng) as s:
            res = partitioning.convert_to_partitioned(
                s, table, months=2, now=_NOW, confirm=table)
            s.commit()
        assert res["verified"] is True and res["rows"] == 2
        # 反向退回普通表（默认也核对），再关掉核对转一次
        with Session(eng) as s:
            back = partitioning.unpartition(s, table, confirm=table, now=_NOW)
            s.commit()
        assert back["verified"] is True and back["rows"] == 2
        # 退回普通表后主键只剩 (tenant_id, event_id)，再转会被前置检查挡下——真实链路上
        # "把 created_day 放进主键"是 alembic 迁移 a9c1d2e3f4b5 的职责。这里手工补上，
        # 好把变量隔离成"verify 开关"这一个。
        with eng.begin() as cc:
            cc.execute(text("ALTER TABLE " + table + " DROP CONSTRAINT pk_" + table))
            cc.execute(text(
                "ALTER TABLE " + table + " ADD CONSTRAINT pk_" + table
                + " PRIMARY KEY (tenant_id, event_id, created_day)"))
        with Session(eng) as s:
            res2 = partitioning.convert_to_partitioned(
                s, table, months=2, now=_NOW, confirm=table, verify=False)
            s.commit()
        assert res2["verified"] is False and res2["rows"] == 2
    finally:
        with eng.begin() as cc:
            for name in (table, table + "_old", table + "_new"):
                cc.execute(text("DROP TABLE IF EXISTS " + name + " CASCADE"))
        eng.dispose()
