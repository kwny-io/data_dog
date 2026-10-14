"""热表按「摄取日桶」分区（架构评审 A9）：`telemetry_events` 从"分批 DELETE"升级为"DROP PARTITION"。

为什么
------
`telemetry_events` 是唯一的**只增**大表（每台设备每秒数条），保留期一到就靠
`repository.delete_in_batches` 一批 500 行地删。DELETE 的问题不是"慢"这么简单：
① 删出来的死元组要 VACUUM 才还空间；② 每次删都要走索引 + 写 WAL，量越大越贵；
③ 表无限增长后，"最近 7 天"的查询也要跟整张表的历史争索引与缓存。
分区把"删数据"变成"删表"：O(1)、不产生死元组、不留 history。这是 PG 上处理时序表的常规做法。

边界（务必读，且与代码一致）
------------------------
- **只在 PostgreSQL 上工作**。SQLite（测试 / 单机演示）走原路径：`is_partitioned()` 返回
  `False`，retention 继续分批 DELETE；需要真 DDL 的函数在非 PG 上抛 `PartitioningUnsupported`。
- **本模块不改表结构**。把**既有**普通表转成分区表在 PG 上不能原地做（建新父表 + 拷数据 +
  重建索引/外键 + 改名切换），属于**需要停机窗口**的迁移。本模块只做**已分区之后**的日常维护
  （建下月分区 / 删过期分区），转换与首次建表见 `docs/storage.md` 的 runbook。
- 分区键固定为 **`created_day`**（摄取日桶 `floor(created_at/86400)`，A9 决策），
  `PARTITION BY RANGE (created_day)`，**分区粒度 = 自然月**（一个分区覆盖该月所有日桶）。
  为什么不是 `created_at`：PG 要求分区表的唯一约束包含分区键，而该表去重键是
  `(tenant_id, event_id)`；`created_day` 由写路径与 `created_at` 同一时刻算出，既满足规则又
  保住**同日**幂等（代价：跨天重投递会多一行，见 `docs/storage.md` §8.2）。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from .models import day_of

PARTITION_TABLE = "telemetry_events"
# 分区键是**摄取日桶** `created_day`（= floor(created_at/86400)），不是 `created_at` 本身：
# PG 要求分区表的唯一约束包含全部分区键，而 `telemetry_events` 的去重键是
# `(tenant_id, event_id)`——把 `created_at` 塞进主键会破坏幂等（重投递带新时间戳）。
# `created_day` 由写路径与 `created_at` 同一时刻算出 → 同日重投递仍去重。代价：跨天重投递
# 会多一行（见 docs/storage.md §8.2）。
PARTITION_KEY = "created_day"

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SUFFIX_RE = re.compile(r"_(\d{4})(\d{2})$")


class PartitioningUnsupported(RuntimeError):
    """当前库不是 PostgreSQL（或目标表还不是分区表）——分区操作不可用。

    这是**能力边界**而不是配置错误：调用方（如 retention）应当把它当"走原路径"的信号，
    而不是当失败重试。
    """


def _dialect(session: Any) -> str:
    return str(session.get_bind().dialect.name)


def is_postgres(session: Any) -> bool:
    """当前会话是否连在 PostgreSQL 上。"""
    return _dialect(session) == "postgresql"


def _check_name(table: str) -> str:
    """表名/分区名只允许 `[A-Za-z_][A-Za-z0-9_]*`——DDL 不能参数化，只能白名单。

    这里的入参全部来自代码常量或 `list_partitions()` 的查询结果，本不需要校验；但 DDL 拼接
    是"今天安全、明天有人把配置接进来"的典型演化型注入，所以**在入口就挡住**。
    """
    if not _NAME_RE.match(table):
        raise ValueError(f"非法标识符（DDL 白名单拒绝）：{table!r}")
    return table


def _day_bounds(start: datetime, end: datetime) -> tuple[int, int]:
    """把"月的起止时刻"换成**日序号**边界（分区键是日桶，不是时间戳）。

    为什么边界不能用时间戳字面量：本仓的时间列是 **epoch 秒的 `double precision`**，真 PG 实测
    `FOR VALUES FROM ('2026-10-01 00:00:00+00')` 会报 `invalid input syntax for type double
    precision`；而且分区键现在根本不是时间列，而是 `floor(created_at/86400)` 的**整数日序号**。
    """
    return day_of(start.timestamp()), day_of(end.timestamp())


def month_bounds(when: datetime) -> tuple[datetime, datetime]:
    """`when` 所在自然月的 `[起, 止)`（止 = 下月 1 日 0 点）。"""
    start = when.astimezone(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, shift_months(start, 1)


def shift_months(when: datetime, months: int) -> datetime:
    """按自然月平移（保留 UTC 与日=1），用于"未来 N 个月"的分区规划。"""
    base = when.astimezone(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    total = base.month - 1 + int(months)
    year = base.year + total // 12
    month = total % 12 + 1
    return base.replace(year=year, month=month)


def partition_name(table: str, when: datetime) -> str:
    """分区名 = `<表>_<YYYYMM>`（例：`telemetry_events_202610`）。"""
    return f"{_check_name(table)}_{when.year:04d}{when.month:02d}"


def create_partition_sql(table: str, start: datetime, end: datetime) -> str:
    """`CREATE TABLE ... PARTITION OF ...`（**纯函数**，离开数据库也能断言）。"""
    name = partition_name(table, start)
    start_day, end_day = _day_bounds(start, end)
    return (f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {_check_name(table)} "
            f"FOR VALUES FROM ({start_day}) TO ({end_day})")


def drop_partition_sql(name: str) -> str:
    """`DROP TABLE ...`（删分区 = 删表；纯函数）。"""
    return f"DROP TABLE IF EXISTS {_check_name(name)}"


def partition_end(name: str) -> float | None:
    """从分区名解析"上界"（**epoch 秒**，= 上界日序号 × 86400）。名字不合规 → None（保守：不删）。

    返回值与 retention 的 `cutoff`（epoch 秒）同量纲，调用方无需知道分区键是日序号。
    """
    m = _SUFFIX_RE.search(name)
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    _, end = month_bounds(datetime(year, month, 1, tzinfo=timezone.utc))
    return day_of(end.timestamp()) * 86400.0


def is_partitioned(session: Any, table: str = PARTITION_TABLE) -> bool:
    """该表在 PG 上是否是**分区父表**（`pg_class.relkind = 'p'`）。非 PG → False。

    刻意"非 PG 返回 False 而不是抛异常"：retention 会用它做分流（False → 原分批 DELETE），
    在 SQLite 上必须能正常走到原路径。
    """
    if not is_postgres(session):
        return False
    row = session.execute(
        text("SELECT relkind FROM pg_class WHERE oid = to_regclass(:t)"),
        {"t": _check_name(table)}).first()
    return bool(row) and str(row[0]) == "p"


def list_partitions(session: Any, table: str = PARTITION_TABLE) -> list[str]:
    """列出该父表的所有分区名（升序）。非 PG → []。"""
    if not is_postgres(session):
        return []
    rows = session.execute(text(
        "SELECT c.relname FROM pg_inherits i "
        "JOIN pg_class c ON c.oid = i.inhrelid "
        "JOIN pg_class p ON p.oid = i.inhparent "
        "WHERE p.relname = :t ORDER BY c.relname"),
        {"t": _check_name(table)}).all()
    return [str(r[0]) for r in rows]


def ensure_partitions(session: Any, table: str = PARTITION_TABLE, *,
                      months: int = 3, now: datetime | None = None) -> list[str]:
    """确保"本月起未来 `months` 个月"的分区都存在（幂等）。返回分区名列表。

    为什么是**未来** N 个月：分区表最大的坑是"忘了建下月分区 → 写入直接报错
    `no partition of relation ... found for row`"。每月维护任务跑这个即可。
    """
    if not is_postgres(session):
        raise PartitioningUnsupported("只有 PostgreSQL 支持分区")
    if not is_partitioned(session, table):
        raise PartitioningUnsupported(
            f"{table} 还不是分区表——先按 docs/storage.md 的 runbook 转换/初次建表")
    base = datetime.now(timezone.utc) if now is None else now
    out: list[str] = []
    for i in range(max(1, int(months))):
        start = shift_months(base, i)
        _, end = month_bounds(start)
        session.execute(text(create_partition_sql(table, start, end)))
        out.append(partition_name(table, start))
    return out


def drop_partitions_before(session: Any, cutoff: float,
                           table: str = PARTITION_TABLE) -> list[str]:
    """删掉**整段都早于 `cutoff`** 的分区（上界 ≤ cutoff）。返回被删的分区名。

    只删"整段过期"的分区：边界月里可能还有要留的数据，宁可少删（保数据），留给下一轮
    或普通删除处理。这正是分区表"删数据"与"删表"的分界。
    """
    if not is_postgres(session):
        raise PartitioningUnsupported("只有 PostgreSQL 支持分区")
    dropped: list[str] = []
    for name in list_partitions(session, table):
        end = partition_end(name)
        if end is None or end > cutoff:
            continue
        session.execute(text(drop_partition_sql(name)))
        dropped.append(name)
    return dropped


def check_partitionable(session: Any, table: str = PARTITION_TABLE,
                        key: str = PARTITION_KEY) -> list[str]:
    """转换**前置检查**：返回阻塞项（空 = 可以转分区表）。

    为什么要有它（真 PG 实测出来的硬规则）：PG 要求**分区表的唯一约束必须包含全部分区键**
    （`unique constraint on partitioned table must include all partitioning columns`）。
    而 `telemetry_events` 的主键是 `(tenant_id, event_id)`——它正是写入路径
    `INSERT ... ON CONFLICT DO UPDATE` 的**去重键**。因此"按 created_at 范围分区 + 保留原主键"
    在 PG 上**不可能**。这个检查把"能不能转"变成可执行的事实，而不是口口相传的注意事项。

    另外也挡住"改名切换"策略的两个坑：**入向外键**（重建时会指向被改名/删除的旧表）与
    分区键列不存在/类型不支持。
    """
    if not is_postgres(session):
        return ["当前库不是 PostgreSQL：分区是 PG 能力，SQLite 走原分批 DELETE"]
    if is_partitioned(session, table):
        return []                                    # 已经是分区表：只剩日常维护
    if session.execute(text("SELECT to_regclass(:t)"), {"t": _check_name(table)}).scalar() is None:
        return [f"表不存在：{table}"]

    blockers: list[str] = []
    col = session.execute(text(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = to_regclass(:t) AND attname = :k AND attnum > 0"),
        {"t": _check_name(table), "k": key}).first()
    if col is None:
        blockers.append(f"分区键列不存在：{table}.{key}")
    else:
        allowed = ("double precision", "real", "integer", "bigint", "smallint",
                   "numeric", "timestamp with time zone", "timestamp without time zone",
                   "date")
        if str(col[0]) not in allowed:
            blockers.append(f"分区键类型不支持范围分区：{table}.{key} 是 {col[0]}")

    pk_rows = session.execute(text(
        "SELECT con.conname, con.contype, "
        "       (SELECT count(*) FROM unnest(con.conkey) AS k "
        "          JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k "
        "         WHERE a.attname = :k) AS key_cols "
        "FROM pg_constraint con "
        "WHERE con.conrelid = to_regclass(:t) AND con.contype IN ('p', 'u')"),
        {"t": _check_name(table), "k": key}).all()
    for name, contype, key_cols in pk_rows:
        if int(key_cols) == 0:
            kind = "主键" if contype == "p" else "唯一约束"
            blockers.append(
                f"{kind} {name} 不含分区键 {key}——PG 要求分区表的唯一约束必须包含全部分区键；"
                "转换前必须改这个键（会改变写入去重语义）")

    fks = session.execute(text(
        "SELECT conrelid::regclass::text, conname FROM pg_constraint "
        "WHERE confrelid = to_regclass(:t)"), {"t": _check_name(table)}).all()
    for from_table, conname in fks:
        blockers.append(f"有入向外键引用它：{from_table}.{conname}（改名切换会让外键指向旧表）")
    return blockers


def describe(session: Any, table: str = PARTITION_TABLE) -> dict[str, Any]:
    """只读快照：是否 PG / 是否分区 / 分区清单 / **转换阻塞项**（CLI `--check` 用）。"""
    pg = is_postgres(session)
    parts = list_partitions(session, table) if pg else []
    return {"dialect": _dialect(session), "postgres": pg,
            "table": table, "partitioned": is_partitioned(session, table),
            "partitions": parts, "partition_count": len(parts),
            "blockers": check_partitionable(session, table)}
