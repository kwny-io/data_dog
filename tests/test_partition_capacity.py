"""容量预检（A9 转换前的第一个门）：离线跑边界，真库跑算术与「父表大小为 0」的陷阱。

为什么单独一个文件：容量预检有一半只能在 PG 上验证（pg_total_relation_size / pg_inherits /
pg_class.reltuples）。和转换本身混在一起测，失败时分不清是「算错了」还是「转换坏了」。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from data_dog.storage import partitioning

_PG_URL = os.environ.get("DD_TEST_PG_URL", "").strip()


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{(tmp_path / 'cap.db').as_posix()}", future=True)
    yield eng
    eng.dispose()


# ---------------------------------------------------------------- 离线（SQLite 边界）

def test_non_pg_reports_unknown_and_does_not_refuse(engine):
    """非 PG 上「测不到」是常态：报告 unknown + 说清原因，但**不拦**（拦了本地部署就死）。"""
    with Session(engine) as s:
        est = partitioning.capacity_estimate(s)
        assert est["measured"] is False
        assert est["source_bytes"] is None and est["need_bytes"] is None
        assert est["rows_estimated"] is True
        assert partitioning.wal_position(s) is None
        assert partitioning.wal_bytes_since(s, None) is None

        rep = partitioning.check_capacity(s)
        assert rep["verdict"] == "unknown"
        assert "容量预检不可用" in rep["message"]
        assert rep["free_source"] == "非 PostgreSQL"


def test_require_free_turns_unknown_into_a_refusal(engine):
    """--require-free-space：不知道还剩多少，就不许动生产表。"""
    with Session(engine) as s:
        with pytest.raises(partitioning.ConversionRefused) as ei:
            partitioning.check_capacity(s, require_free=True)
        assert "require-free-space" in ei.value.blockers[0]
        assert ei.value.details["verdict"] == "unknown"


def test_arithmetic_is_peak_plus_wal(engine, monkeypatch):
    """系数是刻意的保守值：新旧两份（peak）+ 拷贝写出的 WAL。"""
    monkeypatch.setattr(partitioning, "_relation_bytes", lambda _s, _t: 1000)
    monkeypatch.setattr(partitioning, "_relation_rows", lambda _s, _t: 7)
    with Session(engine) as s:
        est = partitioning.capacity_estimate(s)
        assert est["source_bytes"] == 1000 and est["source_rows"] == 7
        assert est["copy_bytes"] == 1000
        assert est["peak_bytes"] == 2000
        assert est["wal_bytes_est"] == 1500
        assert est["need_bytes"] == 3500

        ok = partitioning.check_capacity(s, free_bytes=5000)
        assert ok["verdict"] == "ok"
        assert ok["headroom_bytes"] == 1500
        assert ok["free_source"] == "operator（--free-bytes）"


def test_short_space_refuses_with_the_numbers(engine, monkeypatch):
    """空间不足 → ConversionRefused，且 details 里带缺口（CLI --json 原样可见）。"""
    monkeypatch.setattr(partitioning, "_relation_bytes", lambda _s, _t: 1000)
    monkeypatch.setattr(partitioning, "_relation_rows", lambda _s, _t: 1)
    with Session(engine) as s:
        with pytest.raises(partitioning.ConversionRefused) as ei:
            partitioning.check_capacity(s, free_bytes=10)
        assert ei.value.details["verdict"] == "short"
        assert ei.value.details["shortfall_bytes"] == 3490
        assert "磁盘空间不足" in ei.value.blockers[0]


def test_operator_free_bytes_never_probes_the_filesystem(engine, monkeypatch):
    """给了 --free-bytes 就以操作员为准，不去猜路径（脚本与库常常不在同一台机器）。"""
    def boom(_s):
        raise AssertionError("有 --free-bytes 时不该探测文件系统")

    monkeypatch.setattr(partitioning, "probe_free_bytes", boom)
    monkeypatch.setattr(partitioning, "_relation_bytes", lambda _s, _t: 1000)
    monkeypatch.setattr(partitioning, "_relation_rows", lambda _s, _t: 1)
    with Session(engine) as s:
        rep = partitioning.check_capacity(s, free_bytes=5000)
        assert rep["verdict"] == "ok"
        assert rep["free_source"] == "operator（--free-bytes）"


def test_unknown_free_space_warns_but_does_not_refuse(engine, monkeypatch):
    """测得到需求、测不到余量：默认只警告（把判断交给人），硬门才拒。"""
    monkeypatch.setattr(partitioning, "_relation_bytes", lambda _s, _t: 1000)
    monkeypatch.setattr(partitioning, "_relation_rows", lambda _s, _t: 1)
    monkeypatch.setattr(partitioning, "probe_free_bytes", lambda _s: (None, "非 PostgreSQL"))
    with Session(engine) as s:
        rep = partitioning.check_capacity(s)
        assert rep["verdict"] == "unknown"
        assert rep["free_bytes"] is None
        assert "不拦" in rep["message"]
        with pytest.raises(partitioning.ConversionRefused):
            partitioning.check_capacity(s, require_free=True)


def test_format_bytes_boundaries():
    assert partitioning.format_bytes(None) == "未知"
    assert partitioning.format_bytes(0) == "0 B"
    assert partitioning.format_bytes(999) == "999 B"
    assert partitioning.format_bytes(2048) == "2.0 KiB"
    assert partitioning.format_bytes(3 * 1024**3) == "3.0 GiB"
    assert partitioning.format_bytes(5 * 1024**5) == "5.0 PiB"


# ---------------------------------------------------------------- 真库（PG 才有的一手事实）

def _pg_engine():
    return create_engine(_PG_URL, future=True)


@pytest.mark.postgres
@pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过容量预检真库用例")
def test_real_pg_parent_size_is_zero_so_children_must_be_summed():
    """真 PG：分区父表 pg_total_relation_size 实测为 0，所以必须求子和——这是防回归的关键。"""
    eng = _pg_engine()
    table = "cap_probe"
    try:
        with eng.begin() as c:
            for n in (table, f"{table}_p1", f"{table}_p2"):
                c.execute(text(f"DROP TABLE IF EXISTS {n} CASCADE"))
            c.execute(text(
                f"CREATE TABLE {table} (tenant_id varchar(80) NOT NULL,"
                f" event_id varchar(80) NOT NULL, created_day bigint NOT NULL, payload text)"
                f" PARTITION BY RANGE (created_day)"))
            c.execute(text(f"CREATE TABLE {table}_p1 PARTITION OF {table}"
                           f" FOR VALUES FROM (0) TO (100000)"))
            c.execute(text(f"CREATE TABLE {table}_p2 PARTITION OF {table}"
                           f" FOR VALUES FROM (100000) TO (200000)"))
            c.execute(text(f"INSERT INTO {table} (tenant_id, event_id, created_day, payload)"
                           f" SELECT 't' || g, 'e' || g, g, repeat('x', 100)"
                           f" FROM generate_series(1, 2000) g"))
            c.execute(text(f"ANALYZE {table}"))
        with Session(eng) as s:
            raw = int(s.execute(text("SELECT pg_total_relation_size(to_regclass(:t))"),
                                {"t": table}).scalar() or 0)
            assert raw == 0, ("PG 行为变了：分区父表竟然报了大小——"
                              "_relation_bytes 里「求子和」的前提要重新核对")

            est = partitioning.capacity_estimate(s, table)
            assert est["measured"] is True
            assert est["source_bytes"] and est["source_bytes"] > 0
            assert est["source_rows"] and est["source_rows"] > 0
            assert est["peak_bytes"] == int(est["source_bytes"] * partitioning.DEFAULT_PEAK_FACTOR)
            assert est["wal_bytes_est"] == int(est["source_bytes"] * partitioning.DEFAULT_WAL_RATIO)
            assert est["need_bytes"] == est["peak_bytes"] + est["wal_bytes_est"]

            with pytest.raises(partitioning.ConversionRefused) as ei:
                partitioning.check_capacity(s, table, free_bytes=1)
            assert ei.value.details["verdict"] == "short"
            assert ei.value.details["shortfall_bytes"] == ei.value.details["need_bytes"] - 1
    finally:
        with eng.begin() as c:
            for n in (table, f"{table}_p1", f"{table}_p2"):
                c.execute(text(f"DROP TABLE IF EXISTS {n} CASCADE"))
        eng.dispose()


@pytest.mark.postgres
@pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过容量预检真库用例")
def test_real_pg_probe_free_bytes_and_wal_position_are_readable():
    """真库上余量/来源/WAL 位置的契约：测不到也必须给出可读的原因，不能抛。"""
    eng = _pg_engine()
    try:
        with Session(eng) as s:
            free, source = partitioning.probe_free_bytes(s)
            assert source
            if free is None:
                assert "看不到" in source or "路径" in source or "非" in source
            else:
                assert free > 0 and source.startswith("db_data_dir:")

            pos = partitioning.wal_position(s)
            assert pos is not None
            delta = partitioning.wal_bytes_since(s, pos)
            assert delta is not None and delta >= 0
    finally:
        eng.dispose()
