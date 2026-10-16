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
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from .models import day_of
from .pg_locks import (
    PARTITION_CONVERT_LOCK_KEY,
    PARTITION_ENSURE_LOCK_KEY,
    acquire_xact_lock,
    try_xact_lock,
)

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


class PartitioningBusy(RuntimeError):
    """ensure/drop 的**库层兜底锁**被别的会话持有——本轮跳过（不是错误）。

    只在跨入口租约被**显式关掉**（--no-lease / DD_PARTITION_DDL_LEASE=0）时才会走到
    这里：那时 ensure/drop 没有任何协作式互斥，只能靠 PG 咨询锁硬挡。
    语义与 ConversionBusy 对齐：失败关闭但不排队，调用方报 skipped、退出码 3。
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


def horizon_epoch(session: Any, table: str = PARTITION_TABLE) -> float | None:
    """现有分区覆盖到的最远上界（epoch 秒）；没有分区 / 非 PG → None。

    这是分区表**唯一的提前预警量**：值为 `T` 意味着"到 `T` 之前写入都有地方落"，
    而以日桶为分区键时，越界写入会直接报 `no partition of relation ... found for row`。
    返回 None 表示**无法判断**（非 PG / 分区名不合规），**不是** 0——不要把"不知道"当成"到期了"。
    """
    ends = [partition_end(name) for name in list_partitions(session, table)]
    known = [e for e in ends if e is not None]
    return max(known) if known else None



def _backfill_expr(dialect: str) -> str:
    """`floor(created_at / 86400)` 的可移植写法。

    **必须与迁移 `alembic/versions/20261014_1000_day_bucket_pk.py` 里的公式一致**——
    `tests/test_backfill_created_day.py` 会把两处钉在一起（公式漂移 = 分区键与主键不一致）。
    不能用 `CAST(created_at / 86400 AS BIGINT)` 一把梭：PG 的 double→整型是**四舍五入**，
    正午之后的时刻会被推进次日。
    """
    if dialect == "postgresql":
        return "FLOOR(created_at / 86400)::bigint"
    return "CAST(created_at / 86400 AS INTEGER)"       # SQLite：非负数截断 == floor


def backfill_created_day(session: Any, table: str = PARTITION_TABLE, *, batch: int = 50_000,
                         commit_each: bool = True,
                         log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """给**大表**分批回填 `created_day`（幂等、可中断重跑）：A9 迁移前的停机窗口用。

    为什么需要它：迁移里的回填是**一条 `UPDATE`**——亿级表上就是一个长事务（WAL 峰值高、锁久、
    失败全回滚、只能重头再来），而 `docs/storage.md` 的 runbook 只说"应分批回填"却没工具。

    分批评据是**行标识**（PG `ctid` / SQLite `rowid`），不是主键——回填的正是主键要加的
    那一列，用主键分页会自我循环。每批一次提交（`commit_each=True`），中断后再跑即可：
    它只碰 `created_day IS NULL` 的行。

    返回 `{"updated", "batches", "remaining", "dialect", "batch"}`；`remaining` 是**回填前**观察到的
    待办行数（并发写入可能让它偏保守，以 `--check` 复查为准）。
    """
    dialect = _dialect(session)
    name = _check_name(table)
    expr = _backfill_expr(dialect)
    rowid = "ctid" if dialect == "postgresql" else "rowid"
    size = max(1, int(batch))
    todo = int(session.execute(text(
        f"SELECT count(*) FROM {name} WHERE created_day IS NULL")).scalar() or 0)
    updated = 0
    batches = 0
    while True:
        res = session.execute(text(
            f"UPDATE {name} SET created_day = {expr} WHERE {rowid} IN ("
            f"SELECT {rowid} FROM {name} WHERE created_day IS NULL LIMIT :n)"),
            {"n": size})
        n = int(res.rowcount or 0)
        if commit_each:
            session.commit()
        updated += n
        batches += 1
        if log is not None:
            log(f"created_day 回填：第 {batches} 批 +{n}（累计 {updated}/{todo}）")
        if n < size:
            break
    return {"updated": updated, "batches": batches, "remaining": max(0, todo - updated),
            "dialect": dialect, "batch": size}


def _ensure_fallback_lock(session: Any, lease_off: bool) -> None:
    """租约被显式关掉时的**库层兜底**：非阻塞抢 ensure/drop 的咨询锁，抢不到就抛 PartitioningBusy。

    为什么需要它：多副本 + DD_PARTITION_AUTO_ENSURE=1 + 关租约时，每个副本都会并发执行
    CREATE TABLE IF NOT EXISTS ... PARTITION OF。IF NOT EXISTS **不是并发安全的**：两个会话都通过
    存在性检查后会争 pg_class 的唯一性并让其中一个报错。锁是事务级、随本事务结束释放。

    为什么不复用 PARTITION_CONVERT_LOCK_KEY：转换事务**已经持有**它，而 PG 里同一事务对
    同一把咨询锁再 try 仍返回 False——那会让转换自己判成 busy 而放弃。所以 ensure/drop 有自己的
    钥匙；与转换的冲突由 PG 的表锁兜底（撞上只会被挡、不会坏数据）。
    """
    if not lease_off:
        return                       # 协作式租约还在管事：默认行为逐字不变
    if not is_postgres(session) or try_xact_lock(session, PARTITION_ENSURE_LOCK_KEY):
        return
    raise PartitioningBusy(
        "另一个入口正在做分区 ensure/drop（咨询锁 partition:ddpe 被持有）——本轮跳过")


def ensure_partitions(session: Any, table: str = PARTITION_TABLE, *,
                      months: int = 3, now: datetime | None = None,
                      since: datetime | None = None,
                      lease_off: bool = False) -> list[str]:
    """确保"本月起未来 `months` 个月"的分区都存在（幂等）。返回分区名列表。

    为什么是**未来** N 个月：分区表最大的坑是"忘了建下月分区 → 写入直接报错
    `no partition of relation ... found for row`"。每月维护任务跑这个即可。

    `lease_off=True`（跨入口租约被显式关掉时）额外走库层兜底锁：见 `_ensure_fallback_lock`。
    """
    if not is_postgres(session):
        raise PartitioningUnsupported("只有 PostgreSQL 支持分区")
    if not is_partitioned(session, table):
        raise PartitioningUnsupported(
            f"{table} 还不是分区表——先按 docs/storage.md 的 runbook 转换/初次建表")
    _ensure_fallback_lock(session, lease_off)
    base = datetime.now(timezone.utc) if now is None else now
    out: list[str] = []
    for start in planned_partition_months(since, base, months):
        _, end = month_bounds(start)
        session.execute(text(create_partition_sql(table, start, end)))
        out.append(partition_name(table, start))
    return out


def drop_partitions_before(session: Any, cutoff: float,
                           table: str = PARTITION_TABLE, *,
                           lease_off: bool = False) -> list[str]:
    """删掉**整段都早于 `cutoff`** 的分区（上界 ≤ cutoff）。返回被删的分区名。

    只删"整段过期"的分区：边界月里可能还有要留的数据，宁可少删（保数据），留给下一轮
    或普通删除处理。这正是分区表"删数据"与"删表"的分界。`lease_off=True` 同 `ensure_partitions`。
    """
    if not is_postgres(session):
        raise PartitioningUnsupported("只有 PostgreSQL 支持分区")
    _ensure_fallback_lock(session, lease_off)
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


# ---------------------------------------------------------------- 既有表转换

class ConversionRefused(RuntimeError):
    """前置检查未通过（或目标已是分区表 / 中间表已存在）——**拒绝转换**，附阻塞项。

    拒绝而不是"尽力而为"：转换要在停机窗口里动生产表，任何没讲清楚的前提都必须挡住。

    `details` 可选的**机器可读**上下文（确认令牌、静默窗口实测值等），供 CLI 把
    "为什么被拒" 写进 --json 载荷，而不得靠解析异常文本。
    """

    def __init__(self, blockers: list[str], *, details: dict[str, Any] | None = None) -> None:
        super().__init__(blockers)
        self.blockers = list(blockers)
        self.details: dict[str, Any] = dict(details or {})


class ConversionBusy(RuntimeError):
    """另一个转换正在进行（**库层咨询锁**被持有）——**不排队、直接退出**。

    为什么不像迁移那样阻塞等待：转换会话上有 statement_timeout（默认 15s），"排队"
    实际会在 15 秒后爆成难读的 QueryCanceled。与其让人看不懂，不如当场说
    "另一个转换在跑，这轮不该我"。退出码语义与租约那条腿对齐：**3 = 这轮不该我干**。
    """


# 静默窗口的默认口径：与 `X-Confirm-Tenant`（不可逆删除前要原样复述租户 id）同一类
# “机器可验的前置条件”——不靠 runbook 里念一句“记得停接入”。
DEFAULT_QUIET_SECONDS = 30.0


def _quiet_signal(session: Any, table: str = PARTITION_TABLE, *,
                  now: datetime | None = None) -> dict[str, Any]:
    """只读：目标表最近一次写入离现在多久（秒）——转换的“停接入”前置信号。

    为什么用 `created_at`（epoch 秒）而不是分区键 `created_day`：日桶的分辨率只有一天，
    “30 秒内有没有写”根本看不出来。基准时刻默认取 **库自己的时间**（PG：`extract(epoch from now())`）
    而不是进程时钟：运维终端与库的时钟偏移不该把“刚写过”误判成“已停”。

    返回：{present 有无数据, last_write_age_s 空表时为 None,
    last_write_epoch, checked_at_epoch}。
    """
    name = _check_name(table)
    base = now.timestamp() if now is not None else (
        float(session.execute(text("SELECT extract(epoch from now())")).scalar())
        if is_postgres(session) else datetime.now(timezone.utc).timestamp())
    last = session.execute(text(f"SELECT max(created_at) FROM {name}")).scalar()
    if last is None:
        return {"present": False, "last_write_age_s": None,
                "last_write_epoch": None, "checked_at_epoch": base}
    last_f = float(last)
    return {"present": True, "last_write_age_s": max(0.0, base - last_f),
            "last_write_epoch": last_f, "checked_at_epoch": base}


def check_convert_quiet(session: Any, table: str = PARTITION_TABLE, *, seconds: float,
                        now: datetime | None = None) -> dict[str, Any]:
    """静默窗口门：最近一次写入必须早于 seconds 秒前，否则拒绝转换。

    这是防数据丢失的那一条：`INSERT INTO new SELECT * FROM old` 拿的是读快照，**事务期间的并发
    写入不会进新表**，`RENAME` 那一刻就丢了。互斥只保证“同一时刻一个操作者在动结构”，
    跟“有人在写”毫无关系。把“停接入”从“人记得”变成机器可验的前置条件。

    提醒（如实写进文档）：它在**事务内、拿锁之后、任何 DDL 之前**运行，所以与
    `INSERT ... SELECT` 共享同一事务快照；但它**不能阻止静默窗口之后新到的写入**（那只能靠停服）——
    它只把“我们以为停了”变成“实测停了”。
    """
    sig = _quiet_signal(session, table, now=now)
    age = sig["last_write_age_s"]
    if sig["present"] and (age is None or age < float(seconds)):
        raise ConversionRefused(
            [f"{table} 最近一次写入距今 {age:.1f}s（要求 >= {float(seconds):.1f}s）"
             "——转换会丢掉事务期间的并发写入，请先停接入"],
            details={"reason": "not_quiet", "last_write_age_s": age,
                     "require_quiet_s": float(seconds), "rows": sig["present"]})
    return sig


def check_convert_confirm(table: str, confirm: str | None) -> None:
    """确认令牌门：必须在 confirm 里**原样复述表名**，否则拒绝。

    与 `POST /v1/tenant/purge` 的 `X-Confirm-Tenant` 同意：不可逆动作前要求人把目标写一遍。
    它挡的是“手滑把错表转了”与“脚本被调度器无参数重跑”；它**不**是安全边界
    （能跑这个脚本的人本来就有 DDL 权限）。
    """
    if (confirm or "").strip() != table:
        raise ConversionRefused(
            [f"缺少确认令牌：请用 --confirm-table {table} 原样复述表名"
             "（不可逆的结构变更）"],
            details={"reason": "confirm_required", "expect": table,
                     "got": (confirm or "").strip()})


def _q(ident: str) -> str:
    """把来自系统目录的标识符（策略名/角色名）安全地双引号化。"""
    return '"' + str(ident).replace('"', '""') + '"'


def _months_between(a: datetime, b: datetime) -> int:
    """`a` 到 `b` 相差几个自然月（同月 = 0）。"""
    return (b.year - a.year) * 12 + (b.month - a.month)


def planned_partition_months(since: datetime | None, now: datetime,
                            months: int) -> list[datetime]:
    """要建的分区所属月份：从 `since` 所在月到 `now + months - 1`（含端点）。

    `since` 用于转换时覆盖**既有数据**所在的月份——漏了就会在 `INSERT ... SELECT` 时撞
    `no partition of relation ... found for row`。
    """
    first = shift_months(now if since is None else since, 0)
    span = _months_between(first, now) + max(1, int(months))
    return [shift_months(first, i) for i in range(max(1, span))]


def _index_signatures(session: Any, table: str) -> dict[tuple[bool, tuple[str, ...]], tuple[str, bool]]:
    """索引签名 → (索引名, 是否主键)。签名 = (唯一性, 列名元组)。

    为什么不用 `pg_indexes.indexdef` 文本配对：文本里混着表名/索引名/`ON ONLY` 等方言细节，
    字符串处理容易在某个 PG 版本上悄悄失配；列 + 唯一性才是索引的**实质**。
    表达式索引（`attnum = 0` → 列名为 None）明确报"不支持"，不猜。
    """
    rows = session.execute(text(
        "SELECT c.relname, i.indisunique, i.indisprimary, "
        "       (SELECT array_agg(a.attname ORDER BY k.ord) "
        "          FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) "
        "          JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum) "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE i.indrelid = to_regclass(:t)"), {"t": _check_name(table)}).all()
    out: dict[tuple[bool, tuple[str, ...]], tuple[str, bool]] = {}
    for name, uniq, primary, cols in rows:
        if not cols or any(c is None for c in cols):
            raise ConversionRefused([f"索引 {name} 是表达式索引，转换脚本不支持（不猜）"])
        out[(bool(uniq), tuple(str(c) for c in cols))] = (str(name), bool(primary))
    return out


def _copy_rls(session: Any, src: str, dst: str) -> list[str]:
    """复制 RLS 开关与策略。**`LIKE ... INCLUDING ALL` 不复制它们**（真 PG 实测）。"""
    row = session.execute(text(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = to_regclass(:t)"),
        {"t": _check_name(src)}).first()
    notes: list[str] = []
    if not row or not row[0]:
        return notes
    session.execute(text(f"ALTER TABLE {_check_name(dst)} ENABLE ROW LEVEL SECURITY"))
    if row[1]:
        session.execute(text(f"ALTER TABLE {_check_name(dst)} FORCE ROW LEVEL SECURITY"))
    notes.append("RLS: ENABLE" + (" + FORCE" if row[1] else ""))
    cmds = {"*": "ALL", "r": "SELECT", "a": "INSERT", "w": "UPDATE", "d": "DELETE"}
    pols = session.execute(text(
        "SELECT polname, polcmd, polpermissive, "
        "       pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid), "
        "       (SELECT array_agg(rolname ORDER BY rolname) FROM pg_roles r "
        "         WHERE r.oid = ANY(p.polroles)) "
        "FROM pg_policy p WHERE polrelid = to_regclass(:t) ORDER BY polname"),
        {"t": _check_name(src)}).all()
    for polname, cmd, permissive, using, check, roles in pols:
        parts = [f"CREATE POLICY {_q(polname)} ON {_check_name(dst)}",
                 "AS PERMISSIVE" if permissive else "AS RESTRICTIVE",
                 f"FOR {cmds.get(str(cmd), str(cmd))}"]
        if roles:
            parts.append("TO " + ", ".join(_q(r) for r in roles))
        if using:
            parts.append(f"USING ({using})")
        if check:
            parts.append(f"WITH CHECK ({check})")
        session.execute(text(" ".join(parts)))
        notes.append(f"policy {polname}")
    return notes


def _copy_grants(session: Any, src: str, dst: str) -> list[str]:
    """复制非属主的表级授权（`LIKE` 也不复制授权）。"""
    rows = session.execute(text(
        "SELECT grantee, string_agg(privilege_type, ', ') "
        "FROM information_schema.role_table_grants "
        "WHERE table_name = :t AND grantee <> current_user GROUP BY grantee"),
        {"t": _check_name(src)}).all()
    notes: list[str] = []
    for grantee, privs in rows:
        session.execute(text(f"GRANT {privs} ON {_check_name(dst)} TO {_q(grantee)}"))
        notes.append(f"grant {privs} -> {grantee}")
    return notes


def _data_horizon(session: Any, table: str, now: datetime | None) -> tuple[Any, Any, datetime, datetime | None]:
    """（首日桶, 末日桶, 基准时刻, 起点月）——转换要覆盖既有数据的整个月份区间。"""
    first, last = session.execute(text(
        f"SELECT min(created_day), max(created_day) FROM {_check_name(table)}")).first()
    base = datetime.now(timezone.utc) if now is None else now
    since = (datetime.fromtimestamp(int(first) * 86400, tz=timezone.utc)
             if first is not None else None)
    horizon = base
    if last is not None:
        horizon = max(base, datetime.fromtimestamp(int(last) * 86400, tz=timezone.utc))
    return first, last, horizon, since


def plan_conversion(session: Any, table: str = PARTITION_TABLE, *,
                    months: int = 3, now: datetime | None = None) -> dict[str, Any]:
    """转换计划（**只读**）：能不能转、多少行、要建哪些分区（`--dry-run` 与人工审阅用）。"""
    if not is_postgres(session):
        raise PartitioningUnsupported("只有 PostgreSQL 支持分区")
    blockers = list(check_partitionable(session, table))
    if is_partitioned(session, table):
        blockers.append(f"{table} 已经是分区表")
    plan: dict[str, Any] = {"table": table, "new_table": f"{table}_new",
                            "blockers": blockers, "rows": 0, "partitions": [], "actions": []}
    if blockers:
        return plan
    _, _, horizon, since = _data_horizon(session, table, now)
    plan["rows"] = int(session.execute(text(
        f"SELECT count(*) FROM {_check_name(table)}")).scalar() or 0)
    plan["partitions"] = [partition_name(table, m)
                          for m in planned_partition_months(since, horizon, months)]
    plan["actions"] = [
        f"CREATE TABLE {table}_new (LIKE {table} INCLUDING ALL) PARTITION BY RANGE (created_day)",
        f"建 {len(plan['partitions'])} 个分区（{plan['partitions'][0]} … {plan['partitions'][-1]}）",
        f"INSERT INTO {table}_new SELECT * FROM {table}（{plan['rows']} 行）",
        "复制 RLS 开关与策略、非属主授权",
        f"核对行数后 RENAME 切换，删掉 {table}_old",
    ]
    return plan


def convert_to_partitioned(session: Any, table: str = PARTITION_TABLE, *,
                           months: int = 3, keep_old: bool = False,
                           now: datetime | None = None, confirm: str = "",
                           require_quiet: float | None = None,
                           lock_wait: bool = False) -> dict[str, Any]:
    """把**既有普通表**就地转成按 `created_day` 的月分区表（**单事务**，失败全回滚）。

    为什么敢自动化（此前只说"要人工 runbook"）：日桶进主键之后，
    `CREATE TABLE new (LIKE old INCLUDING ALL) PARTITION BY RANGE (created_day)` 就能建出
    **结构等价**的新表（真 PG 实测：主键/唯一索引/普通索引都会被复制成分区索引）。剩下的差异
    是 `LIKE` **不复制**的三样——RLS 开关、策略、授权——脚本显式复制，并在提交前核对行数。

    仍然是**停机窗口**操作：同一事务里 `INSERT ... SELECT` 拿的是旧表的读快照，期间的并发写入
    不会进新表；`RENAME` 还要 ACCESS EXCLUSIVE。**执行前请停止接入**。

    **前置条件（要求而不是建议）**：

    - `confirm` 必须**原样复述表名**（与 `X-Confirm-Tenant` 同意），否则拒绝；
    - `require_quiet=N` 时，要求最近一次写入早于 N 秒前（在**拿锁之后、任何 DDL 之前**检查，
      与 `INSERT ... SELECT` 同事务），否则拒绝——这是唯一直接降“转换丢并发写入”概率的改动；
    - `lock_wait=False`（默认）时库层咨询锁**不排队**：抢不到抛 `ConversionBusy`，
      避免 15s 后爆成难读的 QueryCanceled。

    返回：`{converted, rows, partitions, notes, old_table, quiet}`。**不 commit**——由调用方决定事务边界。
    """
    # 确认令牌：在**做任何事之前**就拥有最强的语义（连计划都不算）。
    check_convert_confirm(table, confirm)

    plan = plan_conversion(session, table, months=months, now=now)
    if plan["blockers"]:
        raise ConversionRefused(plan["blockers"])
    new = plan["new_table"]

    # 库层硬互斥：与另一个 --convert（或另一个副本的手工转换）**串行**，而不是两边同时
    # RENAME 撞 ACCESS EXCLUSIVE 表锁。事务级锁随本事务结束自动释放，不改变调用方事务边界。
    #
    # **刻意放在“查中间表是否已存在”之前**：锁忙时连一次探测都不做（快失败），
    # 也避免在没有 PG 能力的环境里撞上 PG 专有函数。
    #
    # 为什么 WAIT=False（除非调用方显式要求等）：会话上有 statement_timeout（15s），“排队”
    # 最终是 15 秒后抛 QueryCanceled（看不懂、也分不清是谁占着）。失败关闭但不排队：当场告诉
    # 调用方“另一个转换在跑”，退出码 3 与租约那条腿对齐。
    if lock_wait:
        acquire_xact_lock(session, PARTITION_CONVERT_LOCK_KEY)
    elif not try_xact_lock(session, PARTITION_CONVERT_LOCK_KEY):
        raise ConversionBusy(
            "另一个转换正在进行（咨询锁 partition:ddpc 被持有）——本轮不排队，请稍后重试")

    if is_postgres(session) and session.execute(
            text("SELECT to_regclass(:t)"), {"t": _check_name(new)}).scalar() is not None:
        raise ConversionRefused([f"{new} 已存在——脚本不替你删表，请人工确认后再执行"])

    # 静默窗口：**拿锁之后、任何 DDL 之前**（与 INSERT...SELECT 同事务）。不满足就拒绝，
    # 不要去建一张注定会丢写入的新表。
    quiet = check_convert_quiet(session, table, seconds=require_quiet, now=now) \
        if require_quiet is not None else None

    notes: list[str] = [f"建分区父表 {new}（LIKE {table} INCLUDING ALL）"]
    session.execute(text(f"CREATE TABLE {_check_name(new)} (LIKE {_check_name(table)} INCLUDING ALL) "
                         "PARTITION BY RANGE (created_day)"))
    _, _, horizon, since = _data_horizon(session, table, now)
    names: list[str] = []
    for start in planned_partition_months(since, horizon, months):
        _, end = month_bounds(start)
        session.execute(text(create_partition_sql(new, start, end)))
        names.append(partition_name(new, start))
    notes.append(f"建 {len(names)} 个分区：{names[0]} … {names[-1]}")

    session.execute(text(f"INSERT INTO {_check_name(new)} SELECT * FROM {_check_name(table)}"))
    notes += _copy_rls(session, table, new)
    notes += _copy_grants(session, table, new)

    old_rows = int(session.execute(text(f"SELECT count(*) FROM {_check_name(table)}")).scalar() or 0)
    new_rows = int(session.execute(text(f"SELECT count(*) FROM {_check_name(new)}")).scalar() or 0)
    if old_rows != new_rows:
        raise ConversionRefused(
            [f"行数核对失败（{table}={old_rows}, {new}={new_rows}），已回滚"])
    notes.append(f"行数核对通过：{new_rows}")

    old_sigs = _index_signatures(session, table)
    new_sigs = _index_signatures(session, new)
    old_table = f"{table}_old"
    if keep_old:                       # 腾出规范索引名（索引名是 schema 级唯一；约束名不是）
        for old_name, _primary in old_sigs.values():
            session.execute(text(f"ALTER INDEX {_check_name(old_name)} "
                                 f"RENAME TO {_check_name(old_name + '_old')}"))
    session.execute(text(f"ALTER TABLE {_check_name(table)} RENAME TO {_check_name(old_table)}"))
    session.execute(text(f"ALTER TABLE {_check_name(new)} RENAME TO {_check_name(table)}"))
    for name in names:                 # 分区名去掉 _new 中缀，回到 <表>_<YYYYMM>
        suffix = name[len(new):]
        session.execute(text(f"ALTER TABLE {_check_name(name)} "
                             f"RENAME TO {_check_name(table + suffix)}"))
    if not keep_old:
        # **必须先删旧表**：索引名是 schema 级唯一，旧表还占着 `pk_telemetry_events` 这类规范名，
        # 不改名就撞 `relation ... already exists`（真 PG 实测）。
        session.execute(text(f"DROP TABLE {_check_name(old_table)}"))
        notes.append(f"删掉 {old_table}")
    else:
        notes.append(f"保留旧表 {old_table}（其索引名已加 _old 后缀以腾出规范名）")
    for sig, (old_name, old_primary) in old_sigs.items():
        entry = new_sigs.get(sig)
        if entry is None or entry[0] == old_name:
            continue
        if old_primary:                # 主键：改**约束**名（否则像索引改名那样只改索引）
            session.execute(text(f"ALTER TABLE {_check_name(table)} "
                                 f"RENAME CONSTRAINT {_q(entry[0])} TO {_q(old_name)}"))
        else:
            session.execute(text(f"ALTER INDEX {_check_name(entry[0])} "
                                 f"RENAME TO {_check_name(old_name)}"))
    return {"converted": True, "rows": new_rows,
            "partitions": [table + n[len(new):] for n in names],
            "notes": notes, "old_table": old_table if keep_old else None,
            "quiet": quiet}
