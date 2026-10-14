"""telemetry_events 按月分区（架构评审 A9）：纯函数、非 PG 边界、retention 分流、真 PG 往返。

分层守四件事：
1. **纯函数可离线断言**：分区名 / 月边界 / DDL 文本，不连库就能钉死；
2. **非 PG 是能力边界而不是错误**：is_partitioned 返回 False（retention 走原路径），
   ensure/drop 抛 PartitioningUnsupported；
3. **retention 分流**：已是分区表时删数据走 DROP PARTITION，且不再逐行 DELETE；
4. **真 PG 往返**（CI 的 postgres 腿；本地无 PG 则跳过）：建分区 → 插入 → 删过期分区。
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
from datetime import datetime, timezone

import pytest

from data_dog import retention
from data_dog.storage import partitioning
from data_dog.storage.db import init_db, reset_engine

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 与其它 PG 集成测试同一个开关：设了 DD_TEST_PG_URL 才跑真库用例。
_PG_URL = os.environ.get("DD_TEST_PG_URL", "").strip()

NOW = 1_800_000_000.0
DAY = 86400.0


def _load_script(name: str):
    """scripts/*.py 不是包，用 spec 直接加载（无 stdio 依赖，也更快）。"""
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_dd_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def url(tmp_path, monkeypatch):
    u = f"sqlite:///{(tmp_path / 'part.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", u)
    reset_engine()
    init_db(u)
    yield u
    reset_engine()


# ---------------------------------------------------------------- 纯函数（离线）

def test_partition_name_and_month_bounds():
    d = datetime(2026, 12, 15, tzinfo=timezone.utc)
    start, end = partitioning.month_bounds(d)
    assert start == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert end == datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert partitioning.partition_name("telemetry_events", d) == "telemetry_events_202612"


def test_shift_months_wraps_year():
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert partitioning.shift_months(base, 3) == datetime(2027, 2, 1, tzinfo=timezone.utc)
    assert partitioning.shift_months(base, 14) == datetime(2028, 1, 1, tzinfo=timezone.utc)


def test_create_and_drop_sql_use_epoch_bounds():
    """边界是 **epoch 秒整数**：本仓时间列是 double precision（真 PG 上时间戳字面量会报错）。"""
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    _, end = partitioning.month_bounds(start)
    sql = partitioning.create_partition_sql("telemetry_events", start, end)
    assert sql == (
        "CREATE TABLE IF NOT EXISTS telemetry_events_202610 "
        "PARTITION OF telemetry_events "
        f"FOR VALUES FROM ({int(start.timestamp())}) TO ({int(end.timestamp())})")
    assert partitioning.drop_partition_sql("telemetry_events_202610") == (
        "DROP TABLE IF EXISTS telemetry_events_202610")


def test_ddl_rejects_identifiers_outside_the_whitelist():
    """DDL 不能参数化：入口白名单挡住"今天安全、明天有人把配置接进来"。"""
    for bad in ("telemetry_events; DROP TABLE x", "1bad", "a b", ""):
        with pytest.raises(ValueError):
            partitioning.drop_partition_sql(bad)


def test_partition_end_parses_suffix_and_rejects_odd_names():
    end = partitioning.partition_end("telemetry_events_202610")
    assert end == datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
    assert partitioning.partition_end("telemetry_events_default") is None
    assert partitioning.partition_end("telemetry_events_202613") is None


# ---------------------------------------------------------------- 非 PG 边界

def test_sqlite_reports_not_partitioned_and_lists_nothing(url):
    from data_dog.storage.db import session_scope

    with session_scope(system=True) as s:
        assert partitioning.is_postgres(s) is False
        assert partitioning.is_partitioned(s) is False
        assert partitioning.list_partitions(s) == []
        info = partitioning.describe(s)
    assert info["dialect"] == "sqlite" and info["partitioned"] is False
    assert info["blockers"] and "不是 PostgreSQL" in info["blockers"][0]


def test_ddl_operations_raise_on_non_postgres(url):
    from data_dog.storage.db import session_scope

    with session_scope(system=True) as s:
        with pytest.raises(partitioning.PartitioningUnsupported):
            partitioning.ensure_partitions(s, months=2)
        with pytest.raises(partitioning.PartitioningUnsupported):
            partitioning.drop_partitions_before(s, NOW)


# ---------------------------------------------------------------- CLI（非 PG）

def test_script_check_is_read_only_and_ok_on_sqlite(url):
    mod = _load_script("partition_telemetry")
    assert mod.main(["--check"]) == 0


def test_script_refuses_maintenance_on_non_postgres(url):
    """非 PG 上做分区维护必须**明确拒绝**（退出码 2），不能假装成功。"""
    mod = _load_script("partition_telemetry")
    assert mod.main(["--ensure-months", "2"]) == 2
    assert mod.main(["--drop-before", "2026-08"]) == 2


def test_script_parse_cutoff_accepts_month_and_day():
    mod = _load_script("partition_telemetry")
    a = mod.parse_cutoff("2026-08")
    b = mod.parse_cutoff("2026-08-01")
    assert a == b == datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()


def test_script_planned_partitions_are_future_months():
    mod = _load_script("partition_telemetry")
    now = datetime(2026, 12, 15, tzinfo=timezone.utc)
    assert mod.planned_partitions(2, now=now) == [
        "telemetry_events_202612", "telemetry_events_202701"]


# ---------------------------------------------------------------- retention 分流

def _old_telemetry(n: int) -> list[dict]:
    return [{"tenant_id": "t1", "event_id": f"old-{i}", "device_sn": "SN1",
             "occurred_at": "2026-01-01T00:00:00Z", "component_ref": "leg.hip",
             "created_at": NOW - 30 * DAY} for i in range(n)]


def _seed(url: str, table: str, rows: list[dict]) -> None:
    from sqlalchemy import create_engine

    from data_dog.storage.models import Base

    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(Base.metadata.tables[table].insert(), rows)
    eng.dispose()


def _count(url: str, table: str) -> int:
    from sqlalchemy import create_engine, func, select

    from data_dog.storage.models import Base

    eng = create_engine(url)
    with eng.connect() as c:
        n = c.execute(select(func.count()).select_from(
            Base.metadata.tables[table])).scalar()
    eng.dispose()
    return int(n or 0)


def test_retention_drops_partitions_instead_of_deleting_rows(url, monkeypatch):
    """已是分区表 → telemetry 走 DROP PARTITION，且**不再逐行 DELETE**（行还在）。"""
    _seed(url, "telemetry_events", _old_telemetry(3))
    calls: dict = {}

    def fake_drop(session, cutoff, table=partitioning.PARTITION_TABLE):
        calls["cutoff"] = cutoff
        calls["table"] = table
        return ["telemetry_events_202601"]

    monkeypatch.setattr(partitioning, "is_partitioned",
                        lambda s, t=partitioning.PARTITION_TABLE: True)
    monkeypatch.setattr(partitioning, "drop_partitions_before", fake_drop)

    report = retention.run_once(now=NOW, lease=False)
    row = next(r for r in report["tables"] if r["table"] == "telemetry_events")
    assert row["mode"] == "drop_partition"
    assert row["dropped_partitions"] == ["telemetry_events_202601"]
    assert report["dropped_partitions"] == ["telemetry_events_202601"]
    assert calls["table"] == "telemetry_events"
    assert _count(url, "telemetry_events") == 3, "分区分支不该再逐行 DELETE"


def test_retention_still_deletes_rows_when_not_partitioned(url):
    """非分区（默认）→ 逐行 DELETE 与以前完全一致（分流不改变既有行为）。"""
    _seed(url, "telemetry_events", _old_telemetry(3))
    report = retention.run_once(now=NOW, lease=False)
    row = next(r for r in report["tables"] if r["table"] == "telemetry_events")
    assert row.get("mode") != "drop_partition"
    assert row["deleted"] == 3
    assert report["dropped_partitions"] == []
    assert _count(url, "telemetry_events") == 0


# ---------------------------------------------------------------- 真 PG

@pytest.mark.postgres
@pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 分区集成测试")
def test_partition_roundtrip_on_postgres():
    """真 PG：前置检查通过 → 建分区 → 插入落到分区 → 删过期分区 → 新数据仍可写。

    探针忠实还原本仓的两件事：时间列是 `double precision` epoch；唯一约束必须包含分区键。
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    table = "partition_probe"
    eng = create_engine(_PG_URL, future=True)
    with eng.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        c.execute(text(
            f"CREATE TABLE {table} (tenant_id varchar(80) NOT NULL, "
            "event_id varchar(80) NOT NULL, created_at double precision NOT NULL, v int, "
            "PRIMARY KEY (tenant_id, event_id, created_at)) PARTITION BY RANGE (created_at)"))
    try:
        with Session(eng) as s:
            assert partitioning.is_partitioned(s, table) is True
            assert partitioning.check_partitionable(s, table) == []
            now = datetime(2026, 10, 1, tzinfo=timezone.utc)
            names = partitioning.ensure_partitions(s, table, months=3, now=now)
            assert names == [f"{table}_202610", f"{table}_202611", f"{table}_202612"]
            oct_ts = int(datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp())
            nov_ts = int(datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp())
            s.execute(text(f"INSERT INTO {table} VALUES ('t1','e1',{oct_ts},1)"))
            s.commit()
            cutoff = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
            dropped = partitioning.drop_partitions_before(s, cutoff, table)
            assert dropped == [f"{table}_202610"]
            s.commit()
            assert partitioning.list_partitions(s, table) == [
                f"{table}_202611", f"{table}_202612"]
            s.execute(text(f"INSERT INTO {table} VALUES ('t1','e2',{nov_ts},2)"))
            s.commit()
    finally:
        with eng.begin() as c:
            c.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        eng.dispose()


@pytest.mark.postgres
@pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 分区集成测试")
def test_real_telemetry_shape_is_blocked_by_its_primary_key():
    """**真 PG 实测出来的硬结论**：`telemetry_events` 按 created_at 分区做不到。

    PG 规则：分区表的唯一约束必须包含全部分区键（`unique constraint on partitioned table
    must include all partitioning columns`）。该表主键是 `(tenant_id, event_id)`——正是写入
    路径 `INSERT ... ON CONFLICT DO UPDATE` 的去重键。这条用真库把"不行"钉死，并验证前置
    检查能**点名**是哪个约束挡住的（而不是一句"注意事项"）。
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import NotSupportedError
    from sqlalchemy.orm import Session

    table = "telemetry_like_probe"
    eng = create_engine(_PG_URL, future=True)
    with eng.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        c.execute(text(
            f"CREATE TABLE {table} (tenant_id varchar(80) NOT NULL, "
            "event_id varchar(80) NOT NULL, created_at double precision NOT NULL, "
            "PRIMARY KEY (tenant_id, event_id))"))
    try:
        with Session(eng) as s:
            blockers = partitioning.check_partitionable(s, table)
        assert any("不含分区键 created_at" in b for b in blockers), blockers
        assert any("主键" in b for b in blockers), blockers
        with pytest.raises(NotSupportedError) as e:
            with eng.begin() as c:
                c.execute(text(
                    f"CREATE TABLE {table}_partitioned (LIKE {table} INCLUDING ALL) "
                    "PARTITION BY RANGE (created_at)"))
        assert "must include all partitioning columns" in str(e.value)
    finally:
        with eng.begin() as c:
            c.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
            c.execute(text(f"DROP TABLE IF EXISTS {table}_partitioned CASCADE"))
        eng.dispose()
