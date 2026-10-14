"""数据保留与清理作业（④）：把 `docs/slo.md` §6 声明的保留期**真的执行掉**。

为什么需要它
-----------
§6 声明了"原始日志 7 天 / 规范化指标 90 天 / 审计记录 ≥ 1 年"，但在此之前**没有任何
清理路径**——声明只活在文档里：表的体积只增不减，"保留 7 天"实际是"保留到磁盘满"。
企业采购问的正是"你说了保留 N 天，**谁在删**、删的依据是什么、删了有没有留痕"。

三条设计纪律
-----------
1. **破坏性动作默认关闭**。见 `settings.retention_days()`：原始日志/派生候选/已终态 job
   默认执行（可重灌、且文档已声明）；**告警台账与安全审计默认 0 = 不删**，要删必须显式
   开启——审计"只增不改"是它的全部价值，销毁审计本身是合规风险。
2. **有界批次，不是一条大 DELETE**。逐批 `batch_size` 行、最多 `max_batches` 批，
   每批一个短事务：一条 `DELETE WHERE ts < cutoff` 打在大表上会长时间持锁、把
   热路径的写入堵住（本项目已经因为"同步落库排队"吃过 P99 的亏，见
   `docs/write_path_batching.md`）。批次数用满即停并**如实标记 `capped`**——
   这一轮没清完，下一轮继续，而不是"为了清完把库锁死"。
3. **跨租户必须走 `session_scope(system=True)`**。清理是系统级动作，要跨所有租户删；
   在 PG 上启用 RLS 后，不带系统扫描标志的会话**只看得见零行**——会静默变成"啥也没删"
   （这正是迁移 `b7c8d9e0f1a2` 修的那类静默失效）。故这里显式走系统通道。

删除留痕：真的删了东西时写一条 `retention.purge` 审计（**删了什么、按什么截止时间、
各表多少行**）。"谁删的"与"删的依据"因此可查——审计面（①）在这里承担了
"销毁也要留痕"的那一半。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from . import audit, settings
from .state import Lease, get_state_store

_SECONDS_PER_DAY = 86400.0

# 终态状态闭集（与 `service/investigation_jobs.py` 写库时用的字符串一致）。
# 只清**已终态**：`pending`/`running` 的 job 再老也不能删——那是还没干完的活。
_TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "abandoned")
_TERMINAL_WHERE = "status IN (" + ",".join(f"'{s}'" for s in _TERMINAL_STATUSES) + ")"

# 清理租约的 key（与 file 源 / 升级 sweeper 同一套 Lease 原语，只是名字不同）
_PURGE_LEASE_KEY = "retention:purge"


@dataclass(frozen=True)
class Rule:
    """一条保留规则：哪张表、按哪个时间列、由哪个配置键管、算什么。"""

    table: str
    column: str
    knob: str            # `settings.retention_days()` 的键（多张表可共用一个开关）
    label: str
    extra_where: str = ""  # 附加限定（如"只清已终态 job"）


# 顺序即删除顺序：**子表在前、父表在后**（如 outbox/台账/反馈先于 alerts），
# 否则外键会挡（或留下悬挂行）。
RULES: tuple[Rule, ...] = (
    Rule("telemetry_events", "created_at", "telemetry_events", "原始日志"),
    Rule("candidates", "created_at", "candidates", "规范化候选（派生）"),
    Rule("investigation_job", "finished_at", "investigation_job", "已终态调查 job",
         _TERMINAL_WHERE),
    Rule("investigation_run_log", "created_at", "investigation_run_log",
         "调查运行日志（ReAct 轨迹 / 失败上下文）"),
    Rule("reports", "created_at", "alerts", "调查报告（与告警同生命周期）"),
    Rule("alert_outbox", "created_at", "alerts", "投递 outbox"),
    Rule("alert_delivery", "created_at", "alerts", "投递台账"),
    Rule("alert_feedback", "created_at", "alerts", "告警反馈"),
    Rule("alerts", "created_at", "alerts", "告警（与证据包同生命周期）"),
    Rule("audit_log", "ts", "audit_log", "安全审计（只增）"),
)


def cutoffs(*, now: float | None = None) -> dict[str, float]:
    """各配置键的截止时间（epoch 秒）。`0 天`的键不出现——它表示"不删"。"""
    ts = time.time() if now is None else now
    days = settings.retention_days()
    return {knob: ts - d * _SECONDS_PER_DAY
            for knob, d in days.items() if d > 0}


def plan(*, now: float | None = None) -> list[dict[str, Any]]:
    """**只读**地算出各表"按当前配置会被删掉多少行"。

    运维在真的开清理之前，靠它核对"这个截止时间删的量是不是我预期的"。
    """
    from sqlalchemy import func, select

    from .storage.db import session_scope
    from .storage.models import Base

    ts = time.time() if now is None else now
    days = settings.retention_days()
    out: list[dict[str, Any]] = []
    with session_scope(system=True) as s:
        for rule in RULES:
            d = days.get(rule.knob, 0)
            if d <= 0:
                out.append({"table": rule.table, "label": rule.label, "days": 0,
                            "knob": rule.knob, "off": True, "would_delete": 0})
                continue
            cutoff = ts - d * _SECONDS_PER_DAY
            tbl = Base.metadata.tables[rule.table]
            cond = _condition(tbl, rule, cutoff)
            n = s.execute(select(func.count()).select_from(tbl).where(cond)).scalar()
            out.append({"table": rule.table, "label": rule.label, "days": d,
                        "knob": rule.knob, "off": False, "cutoff": cutoff,
                        "would_delete": int(n or 0)})
    return out


def run_once(*, now: float | None = None, batch_size: int = 500,
             max_batches: int = 200, dry_run: bool = False,
             lease: bool | None = None) -> dict[str, Any]:
    """跑一轮清理。返回报告（含每表删除行数、是否被批次数截断）。

    `dry_run=True` 时只统计不删（等价于 `plan()` 的汇总口径），且**不写审计**、**不抢租约**
    （只读动作没必要互斥）。

    **单持有者租约（多副本）**：真的删之前先抢 `retention:purge`。抢不到 → 本轮**跳过**
    并如实返回 `skipped="lease_held"`（不是失败、不是"删了 0 行"——两者要能区分）。
    理由：多副本同时清会互相锁行、制造 I/O 尖峰，"谁在删"也无人可查。
    `lease=False` 或 `DD_RETENTION_LEASE=0` 可关闭（单机直跑）。
    """
    if not dry_run and (lease if lease is not None else settings.retention_lease()):
        holder = _acquire_purge_lease()
        if holder is None:
            current = get_state_store().get(_PURGE_LEASE_KEY)
            owner = current.get("owner", "") if isinstance(current, dict) else ""
            return {"ok": True, "skipped": "lease_held", "lease_owner": owner or "?",
                    "now": time.time() if now is None else now, "dry_run": False,
                    "batch_size": batch_size, "max_batches": max_batches,
                    "total": 0, "tables": []}
        try:
            return _run_once_locked(now=now, batch_size=batch_size,
                                    max_batches=max_batches, dry_run=dry_run)
        finally:
            holder.release()
    return _run_once_locked(now=now, batch_size=batch_size,
                            max_batches=max_batches, dry_run=dry_run)


def _acquire_purge_lease() -> Lease | None:
    """抢清理租约；抢不到返回 None（调用方当作"本轮跳过"）。"""
    from .state import default_owner, get_state_store

    holder = Lease(get_state_store(), _PURGE_LEASE_KEY, default_owner(),
                   ttl_s=settings.retention_lease_ttl_s())
    return holder if holder.acquire() else None


def _run_once_locked(*, now: float | None = None, batch_size: int = 500,
                     max_batches: int = 200, dry_run: bool = False) -> dict[str, Any]:
    """真正干活的那一半（已持有租约或显式关闭租约时调用）。"""
    from sqlalchemy import func, select

    from .storage import partitioning
    from .storage.db import session_scope
    from .storage.models import Base

    if batch_size < 1 or max_batches < 1:
        raise ValueError("batch_size 与 max_batches 必须 ≥ 1")

    ts = time.time() if now is None else now
    days = settings.retention_days()
    rows: list[dict[str, Any]] = []
    total = 0
    partitions_dropped: list[str] = []

    with session_scope(system=True) as s:
        # A9：telemetry 若已是分区表，删数据改走 DROP PARTITION（O(1)、不留死元组、不动索引）。
        # 非 PG / 未分区 → False → 原分批 DELETE，行为与以前完全一致。
        telemetry_partitioned = partitioning.is_partitioned(
            s, partitioning.PARTITION_TABLE)
        for rule in RULES:
            d = days.get(rule.knob, 0)
            if d <= 0:
                rows.append({"table": rule.table, "label": rule.label, "days": 0,
                             "knob": rule.knob, "off": True, "deleted": 0,
                             "batches": 0, "capped": False})
                continue
            cutoff = ts - d * _SECONDS_PER_DAY
            tbl = Base.metadata.tables[rule.table]
            cond = _condition(tbl, rule, cutoff)

            if (not dry_run and telemetry_partitioned
                    and rule.table == partitioning.PARTITION_TABLE):
                # 整段过期的分区直接删表：不逐行 DELETE、不产生死元组。
                names = partitioning.drop_partitions_before(
                    s, cutoff, partitioning.PARTITION_TABLE)
                partitions_dropped.extend(names)
                rows.append({"table": rule.table, "label": rule.label, "days": d,
                             "knob": rule.knob, "off": False, "cutoff": cutoff,
                             "mode": "drop_partition",
                             "dropped_partitions": names,
                             "deleted": 0, "batches": 0, "capped": False})
                continue

            if dry_run:
                n = int(s.execute(
                    select(func.count()).select_from(tbl).where(cond)).scalar() or 0)
                rows.append({"table": rule.table, "label": rule.label, "days": d,
                             "knob": rule.knob, "off": False, "cutoff": cutoff,
                             "deleted": 0, "would_delete": n, "batches": 0,
                             "capped": False})
                total += n
                continue

            deleted, batches = _purge(s, tbl, cond, batch_size, max_batches)
            capped = batches >= max_batches
            rows.append({"table": rule.table, "label": rule.label, "days": d,
                         "knob": rule.knob, "off": False, "cutoff": cutoff,
                         "deleted": deleted, "batches": batches, "capped": capped})
            total += deleted

    report = {"ok": True, "now": ts, "dry_run": dry_run, "batch_size": batch_size,
              "max_batches": max_batches, "total": total, "tables": rows,
              # A9：本轮的 DROP PARTITION 名单（`total` 只统计逐行删除，分区删除单独列，
              # 避免把"删了几个整分区"混进"删了几行"里制造假精确）。
              "dropped_partitions": partitions_dropped}

    # 真的删了东西才留痕：空跑不留痕（否则周期性任务会把审计刷成噪声）。
    if not dry_run and (total > 0 or partitions_dropped):
        reason = f"按保留期清理，共删除 {total} 行"
        if partitions_dropped:
            reason += f"，另删除 {len(partitions_dropped)} 个过期分区"
        audit.record(action=audit.ACTION_RETENTION_PURGE,
                     outcome=audit.OUTCOME_ALLOWED, target="retention",
                     reason=reason,
                     detail={"batch_size": batch_size, "max_batches": max_batches,
                             "total_deleted": total,
                             "dropped_partitions": partitions_dropped,
                             "tables": [{"table": r["table"], "days": r["days"],
                                         "deleted": r["deleted"],
                                         "capped": r["capped"]}
                                        for r in rows if r["deleted"]]})
    return report


def _condition(tbl: Any, rule: Rule, cutoff: float) -> Any:
    """构造 `列 < cutoff`（+ 可选附加限定）的条件表达式。

    用 SQLAlchemy 表达式而不是拼 SQL 字符串：`extra_where` 是**代码里的常量**（不是
    用户输入），但仍要**编译期就进 SQL**、由方言处理引号/类型，避免"今天安全、明天
    有人把配置接到 extra_where 上"这类演化型注入。
    """
    from sqlalchemy import text

    cond = tbl.c[rule.column] < cutoff
    if rule.extra_where:
        cond = cond & text(rule.extra_where)
    return cond


def _purge(session: Any, tbl: Any, cond: Any, batch_size: int,
           max_batches: int) -> tuple[int, int]:
    """有界批次删除——实现在 `storage/repository.py`，与 ⑥ 的租户级删除共用同一处。"""
    from .storage.repository import delete_in_batches

    return delete_in_batches(session, tbl, cond,
                             batch_size=batch_size, max_batches=max_batches)


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    """`python -m data_dog.retention [--dry-run] [--json]`。

    做成 CLI（而不是常驻线程）：清理是**低频、可外部调度**的作业，交给 cron /
    K8s CronJob 比在应用进程里再养一个循环更好——重启不会漏跑。
    外部调度可能**同时**触发多个副本（重叠的 cron、手工补跑），故真删之前先抢
    `retention:purge` 单持有者租约（`state/lease.py` 同一把原语，`DD_RETENTION_LEASE`，
    默认开）：抢不到就本轮跳过，见 `run_once`。
    """
    import argparse
    import json as _json

    from .core.console import ensure_utf8_console

    # Windows 简体中文控制台按 GBK 编码 stdout，本模块的输出含"→""（未删除）"等符号 →
    # 不兜底会 `UnicodeEncodeError` 直接崩（CI 只跑 Linux，单测全绿也抓不到）。
    # `python -m data_dog.retention` 是 `__main__` 路径，console_script 回调覆盖不到它，
    # 必须在入口自己接——与仓库其余演示脚本同一口径（README「诚实边界」有记录）。
    ensure_utf8_console()

    p = argparse.ArgumentParser(description="数据保留清理作业（④）")
    p.add_argument("--dry-run", action="store_true",
                   help="只统计不删除（先核对量级）")
    p.add_argument("--batch-size", type=int, default=500, help="每批删除行数")
    p.add_argument("--max-batches", type=int, default=200, help="每表单轮最多批数")
    p.add_argument("--json", action="store_true", help="输出 JSON（供调度器采集）")
    args = p.parse_args(argv)

    report = run_once(batch_size=args.batch_size, max_batches=args.max_batches,
                      dry_run=args.dry_run)
    if args.json:
        print(_json.dumps(report, ensure_ascii=False))
        return 0

    if report.get("skipped"):
        # 说清楚是"本轮没干活"而不是"跑了但没东西可清"——两者对运维的含义完全不同。
        print("===== 数据保留清理：本轮跳过 =====")
        print(f"  单持有者租约被 {report.get('lease_owner', '?')} 持有"
              "（多副本同一时刻只允许一个在删）；下一轮再来")
        return 0

    mode = "DRY-RUN（未删除）" if args.dry_run else "已执行"
    print(f"===== 数据保留清理：{mode} =====")
    for r in report["tables"]:
        if r["off"]:
            print(f"  {r['table']:<20} 关闭（{r['knob']}=0，默认不动）")
        elif args.dry_run:
            print(f"  {r['table']:<20} 保留 {r['days']:>4} 天 → 将删除 "
                  f"{r['would_delete']:>6} 行")
        elif r.get("mode") == "drop_partition":
            names = r.get("dropped_partitions") or []
            print(f"  {r['table']:<20} 保留 {r['days']:>4} 天 → 已删除 "
                  f"{len(names)} 个分区（DROP PARTITION）：{', '.join(names) or '无'}")
        else:
            flag = "（本轮到批数上限，未清完）" if r["capped"] else ""
            print(f"  {r['table']:<20} 保留 {r['days']:>4} 天 → 已删除 "
                  f"{r['deleted']:>6} 行 / {r['batches']} 批{flag}")
    line = f"  合计：{report['total']} 行"
    if report.get("dropped_partitions"):
        line += f" + {len(report['dropped_partitions'])} 个分区"
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
