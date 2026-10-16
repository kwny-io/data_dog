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
import json
import os
import pathlib
import time
from datetime import datetime, timezone

import pytest

from data_dog import retention
from data_dog.core import metrics
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


def test_create_and_drop_sql_use_day_bucket_bounds():
    """边界是**日序号整数**（分区键 `created_day`，不是时间戳）。

    这里写死数字（20727 / 20758）而不是用 `day_of()` 现算——否则断言与实现同源，等于没测。
    2026-10-01 → day 20727；2026-11-01 → day 20758。
    """
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    _, end = partitioning.month_bounds(start)
    sql = partitioning.create_partition_sql("telemetry_events", start, end)
    assert sql == (
        "CREATE TABLE IF NOT EXISTS telemetry_events_202610 "
        "PARTITION OF telemetry_events "
        "FOR VALUES FROM (20727) TO (20758)")
    assert partitioning.drop_partition_sql("telemetry_events_202610") == (
        "DROP TABLE IF EXISTS telemetry_events_202610")


def test_ddl_rejects_identifiers_outside_the_whitelist():
    """DDL 不能参数化：入口白名单挡住"今天安全、明天有人把配置接进来"。"""
    for bad in ("telemetry_events; DROP TABLE x", "1bad", "a b", ""):
        with pytest.raises(ValueError):
            partitioning.drop_partition_sql(bad)


def test_partition_end_parses_suffix_and_rejects_odd_names():
    """上界仍以 **epoch 秒**返回（与 retention 的 cutoff 同量纲）：20758 × 86400。"""
    end = partitioning.partition_end("telemetry_events_202610")
    assert end == 20758 * 86400.0 == datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
    assert partitioning.partition_end("telemetry_events_default") is None
    assert partitioning.partition_end("telemetry_events_202613") is None


# ------------------------------------------------- 分区有效期（A9 的提前预警量）

def test_horizon_is_none_when_nothing_is_partitioned(url):
    """没有分区 / 非 PG → None（**不是 0**：不要把"不知道"当成"到期了"）。"""
    from data_dog.storage.db import session_scope

    with session_scope(system=True) as s:
        assert partitioning.horizon_epoch(s) is None


def test_horizon_takes_the_farthest_upper_bound(monkeypatch):
    """多分区取**最远**上界（"覆盖到哪" = 最远的那个），名字不合规的忽略。"""
    monkeypatch.setattr(
        partitioning, "list_partitions",
        lambda _s, _t=partitioning.PARTITION_TABLE: [
            "telemetry_events_202609", "junk", "telemetry_events_202611"])
    assert partitioning.horizon_epoch(object()) == datetime(
        2026, 12, 1, tzinfo=timezone.utc).timestamp()


def test_report_horizon_lives_in_the_service_layer():
    """上报指标的动作归 service（`storage` 是叶子层，不得 import core）。"""
    import importlib

    pm = importlib.import_module("data_dog.service.partition_maintenance")
    assert callable(pm.report_horizon)
    assert not hasattr(partitioning, "report_horizon")



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

    from data_dog.storage.models import Base, day_of

    if table == "telemetry_events":        # 主键含摄取日桶（A9）：种子行按 created_at 补算
        rows = [{**r, "created_day": day_of(r["created_at"])} for r in rows]
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

    探针就是**日桶形态**：`created_day bigint` 进主键、按它 RANGE 分区（与 `telemetry_events`
    迁移后的形态一致）。
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from data_dog.storage.models import day_of

    table = "partition_probe"
    eng = create_engine(_PG_URL, future=True)
    with eng.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        c.execute(text(
            f"CREATE TABLE {table} (created_day bigint NOT NULL, tenant_id varchar(80) NOT NULL, "
            "event_id varchar(80) NOT NULL, v int, "
            "PRIMARY KEY (created_day, tenant_id, event_id)) PARTITION BY RANGE (created_day)"))
    try:
        with Session(eng) as s:
            assert partitioning.is_partitioned(s, table) is True
            assert partitioning.check_partitionable(s, table) == []
            now = datetime(2026, 10, 1, tzinfo=timezone.utc)
            names = partitioning.ensure_partitions(s, table, months=3, now=now)
            assert names == [f"{table}_202610", f"{table}_202611", f"{table}_202612"]
            oct_day = day_of(datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp())
            nov_day = day_of(datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp())
            s.execute(text(f"INSERT INTO {table} VALUES ({oct_day},'t1','e1',1)"))
            s.commit()
            # 落对了分区：数据确实在那个物理分区里（不是只看父表能查到）
            assert s.execute(text(f"SELECT count(*) FROM {table}_202610")).scalar() == 1
            cutoff = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
            dropped = partitioning.drop_partitions_before(s, cutoff, table)
            assert dropped == [f"{table}_202610"]
            s.commit()
            assert partitioning.list_partitions(s, table) == [
                f"{table}_202611", f"{table}_202612"]
            s.execute(text(f"INSERT INTO {table} VALUES ({nov_day},'t1','e2',2)"))
            s.commit()
    finally:
        with eng.begin() as c:
            c.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))


@pytest.mark.postgres
@pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 分区集成测试")
def test_postgres_horizon_advances_with_ensured_partitions():
    """真库：horizon = 最远分区上界；ensure 之后它必须**前进**，且被上报成 Gauge。"""
    from prometheus_client import generate_latest
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    table = "horizon_probe"
    eng = create_engine(_PG_URL, future=True)
    with eng.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        c.execute(text(
            f"CREATE TABLE {table} (created_day bigint NOT NULL, v int, "
            "PRIMARY KEY (created_day)) PARTITION BY RANGE (created_day)"))
    try:
        with Session(eng) as s:
            assert partitioning.horizon_epoch(s, table) is None
            now = datetime(2026, 10, 1, tzinfo=timezone.utc)
            partitioning.ensure_partitions(s, table, months=1, now=now)
            s.commit()
            first = partitioning.horizon_epoch(s, table)
            assert first == datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()

            partitioning.ensure_partitions(s, table, months=3, now=now)
            s.commit()
            from data_dog.service.partition_maintenance import report_horizon

            second = report_horizon(s, table)
            assert second is not None and first < second      # 补齐后有效期**前进**
            assert second == datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()
    finally:
        with eng.begin() as c:
            c.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))

    line = next(ln for ln in generate_latest(metrics.registry).decode().splitlines()
                if ln.startswith("data_dog_partition_end_timestamp_seconds{")
                and 'kind="future"' in ln)
    assert float(line.rsplit(" ", 1)[1]) == second


@pytest.mark.postgres
@pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 分区集成测试")
def test_real_telemetry_shape_is_blocked_by_its_primary_key():
    """**真 PG 实测出来的硬结论**：唯一约束不含分区键 = 转不了。

    PG 规则：分区表的唯一约束必须包含全部分区键（`unique constraint on partitioned table
    must include all partitioning columns`）。A9 之所以走"日桶进主键"就是被这条逼的：日桶
    **不在**主键里的表，无论分区键选 `created_at` 还是 `created_day`，都转不成。这条用真库把
"不行 + 被谁挡住"钉死，而不是留一句"注意事项"。
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
            "created_day bigint NOT NULL, PRIMARY KEY (tenant_id, event_id))"))
    try:
        with Session(eng) as s:
            blockers = partitioning.check_partitionable(s, table)
        assert any("不含分区键 created_day" in b for b in blockers), blockers
        assert any("主键" in b for b in blockers), blockers
        # 转换计划与转换入口也必须**拒绝**（不能只在 check 里提示）
        assert partitioning.plan_conversion(s, table)["blockers"], "计划里也应带阻塞项"
        with pytest.raises(partitioning.ConversionRefused):
            partitioning.convert_to_partitioned(s, table, confirm=table)
        with pytest.raises(NotSupportedError) as e:
            with eng.begin() as c:
                c.execute(text(
                    f"CREATE TABLE {table}_partitioned (LIKE {table} INCLUDING ALL) "
                    "PARTITION BY RANGE (created_day)"))
        assert "must include all partitioning columns" in str(e.value)
    finally:
        with eng.begin() as c:
            c.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
            c.execute(text(f"DROP TABLE IF EXISTS {table}_partitioned CASCADE"))
        eng.dispose()


@pytest.mark.postgres
@pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 分区集成测试")
def test_convert_existing_table_to_day_bucket_partitions():
    """真 PG：既有普通表**就地转换**成分区表——数据、索引、RLS、策略都要跟着走。

    这是 A9 第 3/3 步的核心：`LIKE ... INCLUDING ALL` 复制列/约束/索引，但**不复制**
    RLS 开关、策略、授权（真库实测），转换脚本必须显式补上；并在提交前核对行数。
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from data_dog.storage.models import day_of

    table = "conv_probe"
    now = datetime(2026, 12, 1, tzinfo=timezone.utc)
    day_a = day_of(datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp())
    day_b = day_of(datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp())
    eng = create_engine(_PG_URL, future=True)
    with eng.begin() as c:
        for name in (table, f"{table}_old", f"{table}_new"):
            c.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
        c.execute(text(
            f"CREATE TABLE {table} (tenant_id varchar(80) NOT NULL, event_id varchar(80) NOT NULL, "
            "created_day bigint NOT NULL, created_at double precision NOT NULL, "
            "PRIMARY KEY (tenant_id, event_id, created_day))"))
        c.execute(text(f"CREATE INDEX ix_{table}_tenant_created ON {table} (tenant_id, created_at)"))
        c.execute(text(f"INSERT INTO {table} VALUES "
                       f"('t1','e1',{day_a},1.0), ('t1','e2',{day_b},2.0)"))
        c.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        c.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        c.execute(text(f"CREATE POLICY p_tenant ON {table} "
                       "USING (tenant_id = current_setting('app.tenant_id', true))"))
    try:
        with Session(eng) as s:
            plan = partitioning.plan_conversion(s, table, months=2, now=now)
            assert plan["blockers"] == []
            assert plan["rows"] == 2
            assert plan["partitions"] == [f"{table}_202610", f"{table}_202611",
                                          f"{table}_202612", f"{table}_202701"]
            out = partitioning.convert_to_partitioned(
                s, table, months=2, now=now, confirm=table)
            s.commit()
            assert out["converted"] is True and out["rows"] == 2
            assert partitioning.is_partitioned(s, table) is True
            assert partitioning.list_partitions(s, table) == [
                f"{table}_202610", f"{table}_202611", f"{table}_202612", f"{table}_202701"]
            # 数据落进了正确的物理分区（只查父表看不出这点）
            assert s.execute(text(f"SELECT count(*) FROM {table}_202610")).scalar() == 1
            assert s.execute(text(f"SELECT count(*) FROM {table}_202611")).scalar() == 1
            # RLS 开关与策略：LIKE 不复制，必须由脚本补
            rls = s.execute(text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"),
                {"t": table}).first()
            assert (rls[0], rls[1]) == (True, True)
            assert s.execute(text(
                "SELECT count(*) FROM pg_policy WHERE polrelid = to_regclass(:t)"),
                {"t": table}).scalar() == 1
            # 索引/约束名回到规范名（分区索引自动派生的是 <新表>_... ）
            idx = [r[0] for r in s.execute(text(
                "SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": table})]
            assert f"ix_{table}_tenant_created" in idx, idx
            assert any(name.endswith("_pkey") and not name.endswith("_new_pkey") for name in idx), idx
            # 旧表已删（keep_old=False）
            assert s.execute(text("SELECT to_regclass(:t)"), {"t": f"{table}_old"}).scalar() is None
    finally:
        with eng.begin() as c:
            for name in (table, f"{table}_old", f"{table}_new"):
                c.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
        eng.dispose()


@pytest.mark.postgres
@pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 分区集成测试")
def test_converted_partitions_do_not_keep_the_new_infix_in_index_names():
    """真 PG：转换后**子分区索引名**也不能留中间态 `_new` 中缀。

    为什么单独立一条：上一个用例只查**父表**的 `pg_indexes`，而父表那两处恰好是改对的
    （索引 DDL 名字不变、主键改了**约束**名），于是它对"分区被改名、其**索引**没跟着改"
    是结构性盲区。子索引不会被任何现有代码路径改名，会永久停在
    `<table>_new_<YYYYMM>_<col>_idx`——每个分区 6 条、生产上就是上百条常驻垃圾名。
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from data_dog.storage.models import day_of

    table = "conv_idx_probe"
    now = datetime(2026, 12, 1, tzinfo=timezone.utc)
    day = day_of(datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp())
    eng = create_engine(_PG_URL, future=True)
    with eng.begin() as c:
        for name in (table, f"{table}_old", f"{table}_new"):
            c.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
        c.execute(text(
            f"CREATE TABLE {table} (tenant_id varchar(80) NOT NULL, event_id varchar(80) NOT NULL, "
            "created_day bigint NOT NULL, created_at double precision NOT NULL, "
            "PRIMARY KEY (tenant_id, event_id, created_day))"))
        c.execute(text(f"CREATE INDEX ix_{table}_tenant_created ON {table} (tenant_id, created_at)"))
        c.execute(text(f"CREATE INDEX ix_{table}_created ON {table} (created_at)"))
        c.execute(text(f"INSERT INTO {table} VALUES ('t1','e1',{day},1.0)"))
    try:
        with Session(eng) as s:
            partitioning.convert_to_partitioned(s, table, months=2, now=now, confirm=table)
            s.commit()
            assert partitioning.is_partitioned(s, table) is True
            rows = [r[0] for r in s.execute(text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "AND (tablename = :t OR tablename LIKE :p)"), {"t": table, "p": table + r"\_2%"})]
            bad = [n for n in rows if "_new_" in n]
            assert not bad, f"子分区索引名仍带 _new 中缀：{bad}"
            # 子分区索引本身必须存在且被规划器使用（不是"删掉名字清爽了"）
            assert sum(1 for n in rows if n.startswith(table + "_")) >= 3, rows
    finally:
        with eng.begin() as c:
            for name in (table, f"{table}_old", f"{table}_new"):
                c.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
        eng.dispose()

@pytest.mark.postgres
@pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 分区集成测试")
def test_conversion_lifts_the_session_statement_timeout():
    """真 PG：转换/恢复必须在**本事务内**解除 statement_timeout。

    为什么（100 万行排练实测）：应用连接由 storage/db.py 统一设了 15s 的 statement_timeout，
    而转换里的整表 INSERT...SELECT 是**一条语句**——表一大就被取消，转换直接失败。
    这里把会话超时压到很短，再用一条"必然超时"的语句验证护栏确实生效（不依赖表有多大）。
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    eng = create_engine(_PG_URL, future=True)
    table = "timeout_probe"
    with eng.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        c.execute(text(
            f"CREATE TABLE {table} (tenant_id varchar(80) NOT NULL, event_id varchar(80) NOT NULL, "
            "created_day bigint NOT NULL, created_at double precision NOT NULL, "
            f"CONSTRAINT pk_{table} PRIMARY KEY (tenant_id, event_id, created_day)) "
            "PARTITION BY RANGE (created_day)"))
    try:
        with Session(eng) as s:
            s.execute(text("SET statement_timeout = 200"))
            partitioning._lift_statement_timeout(s)
            # 400ms > 200ms：护栏没生效的话这里会抛 QueryCanceled
            s.execute(text("SELECT pg_sleep(0.4)"))
            s.rollback()
        # 对照：不调用护栏时，同样的睡眠必须被取消（证明这条断言不是空的）
        with Session(eng) as s:
            s.execute(text("SET statement_timeout = 200"))
            with pytest.raises(Exception) as exc:
                s.execute(text("SELECT pg_sleep(0.4)"))
            assert "timeout" in str(exc.value).lower() or "QueryCanceled" in str(exc.value)
            s.rollback()
    finally:
        with eng.begin() as c:
            c.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        eng.dispose()


@pytest.mark.postgres
@pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 分区集成测试")
def test_real_table_writes_land_in_partition_and_retention_drops(monkeypatch):
    """真 PG + 真迁移 + 真写路径：`save_telemetry` 落进当月分区；过期分区被整段 DROP。

    前面几条 PG 用例用的是探针表；这条走**真 `telemetry_events`**，把 A9 的三段接起来：
    alembic 迁移（主键加日桶）→ 转换成分区表 → 应用写路径 → retention 删分区。
    """
    from sqlalchemy import text

    from data_dog.storage import migrations
    from data_dog.storage import repository as R
    from data_dog.storage.db import init_db, reset_engine, session_scope
    from data_dog.storage.models import day_of

    monkeypatch.setenv("DD_DATABASE_URL", _PG_URL)
    reset_engine()
    init_db(_PG_URL)                       # alembic upgrade head（含日桶迁移）
    assert migrations.current_revision(_PG_URL)

    with session_scope(system=True) as s:
        if not partitioning.is_partitioned(s, partitioning.PARTITION_TABLE):
            partitioning.convert_to_partitioned(
                s, partitioning.PARTITION_TABLE, confirm=partitioning.PARTITION_TABLE)
            s.commit()
        assert partitioning.ensure_partitions(s, partitioning.PARTITION_TABLE, months=2)
        s.commit()

    ev = {"event_id": "pg-e2e-1", "device_sn": "SN1", "occurred_at": "2026-10-14T00:00:00Z",
          "component_type": "joint_motor", "component_instance": "hip",
          "metrics": {"temperature_c": 61.0}}
    with session_scope() as s:
        R.save_telemetry(s, [ev], tenant_id="t1")

    now = datetime.now(timezone.utc)
    current = partitioning.partition_name(partitioning.PARTITION_TABLE, now)
    with session_scope(system=True) as s:
        # 写路径落进**当月分区**（只查父表看不出这点）
        assert s.execute(text(f"SELECT count(*) FROM {current}")).scalar() >= 1

    # 过期分区：整段早于 cutoff 的分区被 DROP（而不是逐行 DELETE）
    lo = day_of(datetime(2020, 6, 1, tzinfo=timezone.utc).timestamp())
    hi = day_of(datetime(2020, 7, 1, tzinfo=timezone.utc).timestamp())
    expired = f"{partitioning.PARTITION_TABLE}_202006"
    with session_scope(system=True) as s:
        s.execute(text(f"DROP TABLE IF EXISTS {expired} CASCADE"))
        s.execute(text(f"CREATE TABLE {expired} PARTITION OF {partitioning.PARTITION_TABLE} "
                       f"FOR VALUES FROM ({lo}) TO ({hi})"))
        s.commit()
        assert expired in partitioning.list_partitions(s, partitioning.PARTITION_TABLE)
        dropped = partitioning.drop_partitions_before(
            s, datetime(2020, 8, 1, tzinfo=timezone.utc).timestamp(),
            partitioning.PARTITION_TABLE)
        s.commit()
        assert expired in dropped
        assert expired not in partitioning.list_partitions(s, partitioning.PARTITION_TABLE)

# ------------------------------------------------- DDL 互斥（多副本 / 架构评审 A9）

@pytest.fixture()
def clean_state():
    """每个用例一把干净的共享状态：租约存在进程内单例里，用例之间不能互相串。"""
    from data_dog.state import reset_state_store

    reset_state_store()
    yield
    reset_state_store()


def _hold_ddl_lease(*, owner: str = "other-replica", ttl_s: float = 300.0):
    """模拟「另一个副本正在做结构变更」：以别的 owner 抢住同一把租约。"""
    from data_dog.partition_ddl import PARTITION_DDL_LEASE_KEY
    from data_dog.state import Lease, get_state_store

    holder = Lease(get_state_store(), PARTITION_DDL_LEASE_KEY, owner, ttl_s=ttl_s)
    assert holder.acquire(), "测试自己都没抢到租约"
    return holder


_PG_SHAPE = {"dialect": "postgresql", "postgres": True, "partitioned": True,
             "partitions": ["telemetry_events_202610"], "partition_count": 1}


def _pretend_partitioned_pg(monkeypatch):
    """让 CLI 以为面对的是「已分区的 PG 表」，从而走到真正执行 DDL 的那条路。

    在 SQLite 上跑 CLI 会停在「非 PG → 退出码 2」，永远碰不到互斥分支——那只覆盖了
    「能力边界」，覆盖不了本次新增的互斥。`describe` 是形状的唯一来源，换掉它即可。
    """
    monkeypatch.setattr(partitioning, "describe",
                        lambda _s, table=partitioning.PARTITION_TABLE: dict(_PG_SHAPE))


def test_script_check_takes_no_lease(url, clean_state):
    """只读动作不参与互斥：--check 不该在共享状态里留下锁（也不该去碰它）。"""
    from data_dog.partition_ddl import ddl_lease_holder

    mod = _load_script("partition_telemetry")
    assert mod.main(["--check"]) == 0
    assert ddl_lease_holder() == "", "--check 竟然抢了结构变更锁"


def test_script_skips_ensure_when_another_replica_holds_the_lease(
        url, clean_state, monkeypatch, capsys):
    """别的副本正在改结构 → **一个 DDL 都不执行**、退出码 3、如实报 skipped + 持有者。

    3 是刻意的：0 会让调度器以为「补完了」（其实没有），2 又是「配置错」的专用码。
    """
    mod = _load_script("partition_telemetry")
    _pretend_partitioned_pg(monkeypatch)
    called: list[str] = []
    monkeypatch.setattr(partitioning, "ensure_partitions",
                        lambda *a, **k: called.append("ensure") or [])
    holder = _hold_ddl_lease()
    try:
        code = mod.main(["--ensure-months", "2", "--json"])
    finally:
        holder.release()

    assert code == 3, "抢不到租约必须以 3 退出（不是 0 成功，也不是 2 配置错）"
    assert called == [], "被互斥挡住竟然还是建了分区"
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] == "lease_held"
    assert payload["lease_owner"] == "other-replica"
    assert payload["actions"] == [], "跳过时不该报出任何动作"


def test_script_skips_drop_and_convert_when_blocked(url, clean_state, monkeypatch):
    """互斥覆盖全部三个会改结构的动作，不只是 --ensure-months。"""
    mod = _load_script("partition_telemetry")
    _pretend_partitioned_pg(monkeypatch)
    monkeypatch.setattr(partitioning, "plan_conversion",
                        lambda _s, table=partitioning.PARTITION_TABLE, **_kw:
                        {"table": table, "new_table": table + "_new", "blockers": [],
                         "rows": 0, "partitions": [], "actions": []})
    touched: list[str] = []
    monkeypatch.setattr(partitioning, "drop_partitions_before",
                        lambda *a, **k: touched.append("drop") or [])
    monkeypatch.setattr(partitioning, "convert_to_partitioned",
                        lambda *a, **k: touched.append("convert") or {})
    holder = _hold_ddl_lease()
    try:
        assert mod.main(["--drop-before", "2026-08", "--convert"]) == 3
    finally:
        holder.release()
    assert touched == [], "被互斥挡住竟然还是动了结构"


def test_script_no_lease_flag_bypasses_the_lock(url, clean_state, monkeypatch):
    """--no-lease：单机直跑 / 已确认无并发时的显式退路（多副本下别用）。"""
    mod = _load_script("partition_telemetry")
    _pretend_partitioned_pg(monkeypatch)
    called: list[str] = []
    monkeypatch.setattr(partitioning, "ensure_partitions",
                        lambda *a, **k: called.append("ensure") or [])
    holder = _hold_ddl_lease()
    try:
        assert mod.main(["--ensure-months", "2", "--no-lease"]) == 0
    finally:
        holder.release()
    assert called == ["ensure"]


def test_script_dry_run_neither_takes_nor_respects_the_lease(url, clean_state, monkeypatch):
    """只读动作不参与互斥：dry-run 不抢锁，别人持锁也照常给计划。"""
    from data_dog.partition_ddl import ddl_lease_holder

    mod = _load_script("partition_telemetry")
    _pretend_partitioned_pg(monkeypatch)
    assert mod.main(["--ensure-months", "2", "--dry-run"]) == 0
    assert ddl_lease_holder() == "", "dry-run 竟然留了锁"

    holder = _hold_ddl_lease()
    try:
        assert mod.main(["--drop-before", "2026-08", "--dry-run"]) == 0
    finally:
        holder.release()


def test_script_leaves_no_lease_behind_after_a_successful_run(url, clean_state, monkeypatch):
    """干完就还锁：单副本连续两轮都能跑，别的副本不必等满 TTL。"""
    from data_dog.partition_ddl import ddl_lease_holder

    mod = _load_script("partition_telemetry")
    _pretend_partitioned_pg(monkeypatch)
    monkeypatch.setattr(partitioning, "ensure_partitions", lambda *a, **k: [])
    assert mod.main(["--ensure-months", "2"]) == 0
    assert ddl_lease_holder() == "", "跑完没释放——别的副本要等满 TTL"


# ---------------------------------------------------------------- 转换前置条件（①）与"不排队"互斥（②）

def test_convert_refuses_without_matching_confirm_token(url):
    """确认令牌门：不在 `confirm` 里原样复述表名就拒绝（与 X-Confirm-Tenant 同意）。"""
    from data_dog.storage.db import session_scope

    table = partitioning.PARTITION_TABLE
    for token in ("", "telemetry_event", "Telemetry_Events"):
        with session_scope(system=True) as s:
            with pytest.raises(partitioning.ConversionRefused) as e:
                partitioning.convert_to_partitioned(s, table, confirm=token)
            assert e.value.details["reason"] == "confirm_required"
            assert e.value.details["expect"] == table


def test_convert_confirm_check_is_exact():
    """令牌校验是纯函数：完全相等才放行（不做大小写/空格宽容）。"""
    table = "telemetry_events"
    partitioning.check_convert_confirm(table, table)
    # 与 X-Confirm-Tenant 一致：只 strip 首尾空白，**不做大小写宽容**。
    partitioning.check_convert_confirm(table, "  " + table + "  ")
    for token in ("telemetry_event", "Telemetry_Events", "TENANT_EVENTS", ""):
        with pytest.raises(partitioning.ConversionRefused):
            partitioning.check_convert_confirm(table, token)


def test_quiet_gate_passes_on_empty_and_old_table(url):
    """空表与"早已停写"的表都算静默：没有写入就不必等。"""
    from data_dog.storage.db import session_scope
    from data_dog.storage.models import day_of

    table = partitioning.PARTITION_TABLE
    base = datetime(2026, 12, 1, tzinfo=timezone.utc)
    with session_scope(system=True) as s:
        sig = partitioning.check_convert_quiet(s, table, seconds=30.0, now=base)
        assert sig["present"] is False and sig["last_write_age_s"] is None

    old = {"tenant_id": "t1", "event_id": "old-1", "device_sn": "SN1",
           "occurred_at": "2026-10-14T00:00:00Z", "component_ref": "leg.hip",
           "created_at": base.timestamp() - 120.0}
    old["created_day"] = day_of(old["created_at"])
    _seed(url, table, [old])
    with session_scope(system=True) as s:
        sig = partitioning.check_convert_quiet(s, table, seconds=30.0, now=base)
        assert sig["present"] is True and abs(sig["last_write_age_s"] - 120.0) < 0.01
        assert partitioning._quiet_signal(s, table, now=base)["last_write_epoch"] == old["created_at"]


def test_quiet_gate_refuses_when_a_write_landed_inside_the_window(url, monkeypatch):
    """窗口内有写入 → 拒绝，并把"多久前写的、要求多少"写进机器可读 details。"""
    from data_dog.storage.db import session_scope
    from data_dog.storage.models import day_of

    table = partitioning.PARTITION_TABLE
    base = datetime(2026, 12, 1, tzinfo=timezone.utc)
    # plan_conversion 在非 PG 上会先报"能力边界"；这里只测静默门本身。
    monkeypatch.setattr(partitioning, "plan_conversion",
                        lambda _s, t=table, **_kw: {
                            "table": t, "new_table": t + "_new", "blockers": [],
                            "rows": 0, "partitions": [], "actions": []})
    fresh = {"tenant_id": "t1", "event_id": "fresh-1", "device_sn": "SN1",
             "occurred_at": "2026-10-14T00:00:00Z", "component_ref": "leg.hip",
             "created_at": base.timestamp() - 5.0}
    fresh["created_day"] = day_of(fresh["created_at"])
    _seed(url, table, [fresh])

    with session_scope(system=True) as s:
        age = partitioning._quiet_signal(s, table, now=base)["last_write_age_s"]
        assert abs(age - 5.0) < 0.01
        with pytest.raises(partitioning.ConversionRefused) as e:
            partitioning.convert_to_partitioned(
                s, table, now=base, require_quiet=30.0, confirm=table)
        d = e.value.details
        assert d["reason"] == "not_quiet" and d["require_quiet_s"] == 30.0
        assert abs(d["last_write_age_s"] - 5.0) < 0.01


def test_convert_busy_is_raised_instead_of_blocking(url, monkeypatch):
    """②：库层咨询锁抢不到时抛 ConversionBusy（**不排队**），而不是挂到 statement_timeout。"""
    from data_dog.storage.db import session_scope

    table = partitioning.PARTITION_TABLE
    monkeypatch.setattr(partitioning, "plan_conversion",
                        lambda _s, t=table, **_kw: {
                            "table": t, "new_table": t + "_new", "blockers": [],
                            "rows": 0, "partitions": [], "actions": []})
    monkeypatch.setattr(partitioning, "try_xact_lock", lambda _s, _k: False)
    with session_scope(system=True) as s:
        with pytest.raises(partitioning.ConversionBusy):
            partitioning.convert_to_partitioned(s, table, confirm=table)


def test_script_convert_requires_confirm_token(url, clean_state, monkeypatch, capsys):
    """CLI：缺 --confirm-table → 退出码 2，且**碰都没碰** convert_to_partitioned。"""
    mod = _load_script("partition_telemetry")
    _pretend_partitioned_pg(monkeypatch)
    monkeypatch.setattr(partitioning, "plan_conversion",
                        lambda _s, table=partitioning.PARTITION_TABLE, **_kw:
                        {"table": table, "new_table": table + "_new", "blockers": [],
                         "rows": 0, "partitions": [], "actions": []})
    touched: list[str] = []
    monkeypatch.setattr(partitioning, "convert_to_partitioned",
                        lambda *a, **k: touched.append("convert") or {})

    code = mod.main(["--convert", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2, "缺确认令牌必须以 2 退出（不该执行）"
    assert touched == [], "没通过确认门竟然还是调了转换"
    assert payload["refused"]["reason"] == "confirm_required"


def test_script_convert_runs_with_confirm_token(url, clean_state, monkeypatch, capsys):
    """CLI：带上原样表名的确认令牌才真的执行转换。"""
    mod = _load_script("partition_telemetry")
    _pretend_partitioned_pg(monkeypatch)
    monkeypatch.setattr(partitioning, "plan_conversion",
                        lambda _s, table=partitioning.PARTITION_TABLE, **_kw:
                        {"table": table, "new_table": table + "_new", "blockers": [],
                         "rows": 0, "partitions": [], "actions": []})
    seen: dict = {}

    def fake_convert(_s, table=partitioning.PARTITION_TABLE, **kw):
        seen.update(kw)
        return {"notes": ["ok"], "partitions": [table + "_202612"], "quiet": None}

    monkeypatch.setattr(partitioning, "convert_to_partitioned", fake_convert)
    assert mod.main(["--convert", "--confirm-table", partitioning.PARTITION_TABLE]) == 0
    assert seen["confirm"] == partitioning.PARTITION_TABLE
    assert seen["require_quiet"] is None


def test_script_convert_busy_maps_to_exit_3(url, clean_state, monkeypatch, capsys):
    """②：库层锁忙 → CLI 报 skipped=convert_busy + 退出码 3（与租约同语义，不是错误）。"""
    mod = _load_script("partition_telemetry")
    _pretend_partitioned_pg(monkeypatch)
    monkeypatch.setattr(partitioning, "plan_conversion",
                        lambda _s, table=partitioning.PARTITION_TABLE, **_kw:
                        {"table": table, "new_table": table + "_new", "blockers": [],
                         "rows": 0, "partitions": [], "actions": []})

    def busy(*_a, **_k):
        raise partitioning.ConversionBusy("另一个转换在跑")

    monkeypatch.setattr(partitioning, "convert_to_partitioned", busy)
    code = mod.main(["--convert", "--confirm-table", partitioning.PARTITION_TABLE, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["skipped"] == "convert_busy"
    assert payload["actions"] == []


def test_script_convert_dry_run_reports_quiet_without_refusing(url, clean_state, monkeypatch, capsys):
    """dry-run 是只读：--require-quiet 不满足时**不拒绝**，只把静默信号报出来供人判断。"""
    mod = _load_script("partition_telemetry")
    _pretend_partitioned_pg(monkeypatch)
    monkeypatch.setattr(partitioning, "plan_conversion",
                        lambda _s, table=partitioning.PARTITION_TABLE, **_kw:
                        {"table": table, "new_table": table + "_new", "blockers": [],
                         "rows": 0, "partitions": [], "actions": ["INSERT ... SELECT"]})
    def not_quiet(_s, table, *, seconds, now=None):
        raise partitioning.ConversionRefused(
            ["最近 1.0s 内有写入"],
            details={"reason": "not_quiet", "last_write_age_s": 1.0,
                     "require_quiet_s": seconds, "rows": True})

    monkeypatch.setattr(partitioning, "check_convert_quiet", not_quiet)
    code = mod.main(["--convert", "--dry-run", "--require-quiet", "30", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["quiet_ok"] is False, "dry-run 不该因为静默窗口不满足而拒绝"
    assert payload["quiet"]["reason"] == "not_quiet"

@pytest.mark.postgres
@pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 分区集成测试")
def test_convert_busy_on_real_pg_does_not_queue():
    """真 PG：另一个事务持着转换咨询锁时，**立刻**抛 ConversionBusy（不阻塞到 statement_timeout）。"""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from data_dog.storage import pg_locks
    from data_dog.storage.models import day_of

    table = "busy_probe"
    day = day_of(datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp())
    eng = create_engine(_PG_URL, future=True)
    with eng.begin() as c:
        for name in (table, f"{table}_old", f"{table}_new"):
            c.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
        c.execute(text(
            f"CREATE TABLE {table} (tenant_id varchar(80) NOT NULL, event_id varchar(80) NOT NULL, "
            "created_day bigint NOT NULL, created_at double precision NOT NULL, "
            "PRIMARY KEY (tenant_id, event_id, created_day))"))
        c.execute(text(f"INSERT INTO {table} VALUES ('t1','e1',{day},1.0)"))
    try:
        with Session(eng) as a:
            assert pg_locks.acquire_xact_lock(a, pg_locks.PARTITION_CONVERT_LOCK_KEY) is True
            with Session(eng) as b:
                # **计时**：非阻塞是这里唯一真正值钱的性质——一旦退回 wait=True，B 会一直等到
                # statement_timeout（本仓默认 15s）才抛 QueryCanceled，那正是要消灭的"看不懂的超时"。
                t0 = time.monotonic()
                with pytest.raises(partitioning.ConversionBusy):
                    partitioning.convert_to_partitioned(
                        b, table, now=datetime(2026, 12, 1, tzinfo=timezone.utc), confirm=table)
                elapsed = time.monotonic() - t0
                assert elapsed < 2.0, f"抢不到锁时阻塞了 {elapsed:.1f}s（应立刻返回）"
                b.rollback()
            a.commit()                       # 释放锁
        with Session(eng) as c:
            out = partitioning.convert_to_partitioned(
                c, table, now=datetime(2026, 12, 1, tzinfo=timezone.utc), confirm=table)
            c.commit()
            assert out["converted"] is True and partitioning.is_partitioned(c, table)
    finally:
        with eng.begin() as c:
            for name in (table, f"{table}_old", f"{table}_new"):
                c.execute(text(f"DROP TABLE IF EXISTS {name} CASCADE"))
        eng.dispose()


# ---------------------------------------------------------------- ensure/drop 的库层兜底（--no-lease）

def test_script_passes_lease_off_to_ensure_and_drop(url, clean_state, monkeypatch):
    """--no-lease 时 CLI 要把 lease_off=True 传下去，库层才知道该走兜底锁。"""
    mod = _load_script("partition_telemetry")
    _pretend_partitioned_pg(monkeypatch)
    seen: list[dict] = []
    monkeypatch.setattr(
        partitioning, "ensure_partitions",
        lambda _s, _t=partitioning.PARTITION_TABLE, **kw: seen.append(kw) or [])
    monkeypatch.setattr(
        partitioning, "drop_partitions_before",
        lambda _s, _c, _t=partitioning.PARTITION_TABLE, **kw: seen.append(kw) or [])

    assert mod.main(["--ensure-months", "2", "--no-lease"]) == 0
    assert mod.main(["--drop-before", "2026-01", "--no-lease"]) == 0
    assert seen and all(kw.get("lease_off") is True for kw in seen), seen


def test_script_ddl_busy_maps_to_exit_3(url, clean_state, monkeypatch, capsys):
    """兜底锁被占 → skipped=ddl_busy + 退出码 3（与 convert_busy 同语义，不是错误）。"""
    mod = _load_script("partition_telemetry")
    _pretend_partitioned_pg(monkeypatch)

    def busy(*_a, **_kw):
        raise partitioning.PartitioningBusy("另一个入口正在做分区 ensure/drop")

    monkeypatch.setattr(partitioning, "ensure_partitions", busy)
    code = mod.main(["--ensure-months", "2", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["skipped"] == "ddl_busy"
    assert payload["actions"] == []

# ---------------------------------------------------------------- 恢复路径 CLI

def test_recover_script_refuses_without_confirm_token(url, clean_state, monkeypatch, capsys):
    """恢复 CLI：缺少确认令牌 → 退出码 2（不可逆动作前必须复述表名）。"""
    mod = _load_script("unpartition_telemetry")
    _pretend_partitioned_pg(monkeypatch)
    called: list[dict] = []
    monkeypatch.setattr(partitioning, "unpartition",
                        lambda _s, _t=partitioning.PARTITION_TABLE, **kw: called.append(kw) or {})
    code = mod.main(["--unpartition", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["error"] and "前置条件" in payload["error"]
    assert called == [], "确认门没拦住：库层被调用了"


def test_recover_script_dry_run_executes_nothing(url, clean_state, monkeypatch, capsys):
    """`--dry-run` 是只读：打印计划，一个 DDL 都不执行。"""
    mod = _load_script("unpartition_telemetry")
    _pretend_partitioned_pg(monkeypatch)
    called: list[dict] = []
    monkeypatch.setattr(partitioning, "unpartition",
                        lambda _s, _t=partitioning.PARTITION_TABLE, **kw: called.append(kw) or {})
    code = mod.main(["--unpartition", "--dry-run", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and called == []
    assert payload["actions"][0]["unpartition"]


@pytest.mark.postgres
@pytest.mark.skipif(not _PG_URL, reason="未设 DD_TEST_PG_URL，跳过 PG 恢复 CLI 集成测试")
def test_recover_script_end_to_end_on_real_pg():
    """真 PG：转换后跑恢复 CLI，表回到普通表且主键/行数不变。"""
    import subprocess
    import sys

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from data_dog.storage.models import day_of

    table = partitioning.PARTITION_TABLE
    day = day_of(datetime(2026, 11, 15, tzinfo=timezone.utc).timestamp())
    env = dict(os.environ, DD_DATABASE_URL=_PG_URL)
    eng = create_engine(_PG_URL, future=True)
    with eng.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        c.execute(text(
            f"CREATE TABLE {table} (tenant_id varchar(80) NOT NULL, event_id varchar(80) NOT NULL, "
            "created_day bigint NOT NULL, created_at double precision NOT NULL, "
            f"CONSTRAINT pk_{table} PRIMARY KEY (tenant_id, event_id, created_day))"))
        c.execute(text(f"INSERT INTO {table} VALUES ('t1','e1',{day},1.0)"))
    try:
        with Session(eng) as s:
            partitioning.convert_to_partitioned(
                s, table, months=2, now=datetime(2027, 1, 5, tzinfo=timezone.utc), confirm=table)
            s.commit()
            assert partitioning.is_partitioned(s, table) is True
        p = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "unpartition_telemetry.py"),
             "--unpartition", "--confirm-table", table, "--json"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=env)
        assert p.returncode == 0, (p.stdout, p.stderr)
        out = json.loads(p.stdout)
        assert out["partitioned"] is False and out["rows"] == 1
        with Session(eng) as s:
            assert partitioning.is_partitioned(s, table) is False
            pk = s.execute(text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = to_regclass(:t) AND contype = 'p'"), {"t": table}).scalar()
            assert pk == "PRIMARY KEY (tenant_id, event_id)", pk
            assert s.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 1
    finally:
        with eng.begin() as c:
            c.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        eng.dispose()
