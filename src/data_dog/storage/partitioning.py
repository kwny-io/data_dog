"""热表按月分区（架构评审 A9）：`telemetry_events` 从"分批 DELETE"升级为"DROP PARTITION"。

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
- 分区键固定为 `created_at`（表的时间列），`PARTITION BY RANGE (created_at)`，分区粒度=自然月。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

PARTITION_TABLE = "telemetry_events"
PARTITION_KEY = "created_at"

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


def _ts(dt: datetime) -> str:
    """分区边界字面量：显式 UTC 偏移，避免 PG 按会话时区解释成别的时间点。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00")


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
    return (f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {_check_name(table)} "
            f"FOR VALUES FROM ('{_ts(start)}') TO ('{_ts(end)}')")


def drop_partition_sql(name: str) -> str:
    """`DROP TABLE ...`（删分区 = 删表；纯函数）。"""
    return f"DROP TABLE IF EXISTS {_check_name(name)}"


def partition_end(name: str) -> float | None:
    """从分区名解析"上界"（epoch 秒）。名字不合 `<表>_<YYYYMM>` → None（保守：不删）。"""
    m = _SUFFIX_RE.search(name)
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    _, end = month_bounds(datetime(year, month, 1, tzinfo=timezone.utc))
    return end.timestamp()


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


def describe(session: Any, table: str = PARTITION_TABLE) -> dict[str, Any]:
    """体检用的只读快照：是否 PG / 是否分区 / 分区清单（`data-dog partition --check`）。"""
    pg = is_postgres(session)
    parts = list_partitions(session, table) if pg else []
    return {"dialect": _dialect(session), "postgres": pg,
            "table": table, "partitioned": is_partitioned(session, table),
            "partitions": parts, "partition_count": len(parts)}
