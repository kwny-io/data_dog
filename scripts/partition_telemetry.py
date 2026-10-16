"""`telemetry_events` 分区维护 CLI（架构评审 A9）。

三个子用途（都要求 PostgreSQL；非 PG 会**说清楚并退出**，而不是假装成功）：

    python scripts/partition_telemetry.py --check                 # 只读：是否分区 / 有哪些分区
    python scripts/partition_telemetry.py --ensure-months 3       # 建"本月起未来 3 个月"分区（幂等）
    python scripts/partition_telemetry.py --drop-before 2026-08   # 删"整段早于 2026-08"的分区
    python scripts/partition_telemetry.py --convert --dry-run     # 存量普通表 → 分区表：先看计划 + 静默信号
    python scripts/partition_telemetry.py --convert --confirm-table telemetry_events --require-quiet 30

`--convert` 是**停机窗口**操作：单事务里建新分区父表 → 拷数据 → 复制 RLS/策略/授权 → 核对行数 →
改名切换 → 删旧表。失败整体回滚，旧表不动。**执行前停止接入**（事务里拷的是读快照，期间的
并发写入不会进新表）。前置检查不通过（`--check` 会列阻塞项）时**拒绝执行**、退出码 2。

`--convert` 还有两道**机器可验的前置条件**（把"记得停接入"从 runbook 口头约定变成硬门）：

- `--confirm-table <表名>`：必须**原样复述**目标表名（与 `POST /v1/tenant/purge` 的
  `X-Confirm-Tenant` 同意），不一致/缺省 → 拒绝（退出码 2）；
- `--require-quiet N`：要求目标表最近一次写入早于 N 秒前（在拿锁后、任何 DDL 前检查）。
  不满足 → 拒绝（退出码 2）。`--dry-run` 下不拒绝，只把静默信号报出来供人判断。

退出码总览：`0` 干完了 / `2` 前置条件或能力边界不满足（不该执行）/ `3` 这轮**不该我干**
（租约被别人持有，或库层咨询锁告诉你另一个转换在跑）——都不是失败，下一轮再来。

**多副本 / 多入口互斥（架构评审 A9 的收口）**：所有会改结构的动作（`--ensure-months` /
`--drop-before` / `--convert`）先抢 `partition:ddl` 单持有者租约（`data_dog.partition_ddl`），
与"应用启动自动补齐"共用同一把锁。抢不到 → **一个 DDL 都不执行**，如实标
`skipped=lease_held` + `lease_owner`，**退出码 3**（不是 0：调度器据此知道"这轮没干成活"；
也不是 2：什么错都没有，下一轮再来即可）。`--convert` 另外在事务里拿一把 PG 咨询锁
（`storage/pg_locks.py`）：抢不到就报 `skipped=convert_busy`、退出码 3，**不排队**——
会话上的 `statement_timeout` 会让"排队"变成 15 秒后一个看不懂的 QueryCanceled。

只读动作（`--check` / `--dry-run`）**不参与互斥**：不抢锁，别人持锁也照常给出现状与计划。
单机直跑（或已在维护窗里确认无并发）可用 `--no-lease` / `DD_PARTITION_DDL_LEASE=0` 关掉它——
那时保护就只剩"单事务 + 文档化的维护窗"这一层。

为什么不塞进 `retention`：分区维护是**结构变更**（DDL），保留清理是**删数据**。把 DDL 和周期
删除混进同一个定时任务，出问题时没法只暂停其中一个。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any

from data_dog.core import metrics
from data_dog.core.console import ensure_utf8_console
from data_dog.partition_ddl import ddl_guard, record_skip


def parse_cutoff(raw: str) -> float:
    """`YYYY-MM` 或 `YYYY-MM-DD`（按 UTC 解释）→ epoch 秒。"""
    text = raw.strip()
    fmt = "%Y-%m" if len(text) == 7 else "%Y-%m-%d"
    try:
        dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        raise SystemExit(f"无法解析时间：{raw!r}（要 YYYY-MM 或 YYYY-MM-DD）") from None
    return dt.timestamp()


def planned_partitions(months: int, now: datetime | None = None) -> list[str]:
    """ "本月起未来 N 个月"的分区名（与 `ensure_partitions` 同口径，用于 --dry-run）。"""
    from data_dog.storage import partitioning

    base = datetime.now(timezone.utc) if now is None else now
    out = []
    for i in range(max(1, int(months))):
        start = partitioning.shift_months(base, i)
        out.append(partitioning.partition_name(partitioning.PARTITION_TABLE, start))
    return out

def main(argv: list[str] | None = None) -> int:
    ensure_utf8_console()
    p = argparse.ArgumentParser(description="telemetry_events 分区维护（A9）")
    p.add_argument("--check", action="store_true", help="只读：打印分区现状")
    p.add_argument("--ensure-months", type=int, default=0, metavar="N",
                   help="确保本月起未来 N 个月的分区存在（幂等）")
    p.add_argument("--drop-before", default="", metavar="YYYY-MM",
                   help="删除『整段早于该时间』的分区")
    p.add_argument("--convert", action="store_true",
                   help="把既有普通表就地转成分区表（停机窗口；失败整体回滚）")
    p.add_argument("--keep-old", action="store_true",
                   help="转换后保留旧表（<表>_old），便于人工复核后再删")
    p.add_argument("--confirm-table", default="", metavar="表名",
                   help="原样复述目标表名才允许转换（与 X-Confirm-Tenant 同意）")
    p.add_argument("--require-quiet", type=float, default=None, metavar="秒",
                   help="要求转换前最近一次写入早于 N 秒（拒绝并发写入丢失）")
    p.add_argument("--no-verify", action="store_true",
                   help="跳过转换后的整表内容指纹核对（只留行数核对）。默认核对——"
                        "行数相等不等于内容相等（错列/截断都能让行数照样相等）")
    p.add_argument("--dry-run", action="store_true", help="只打印将要做的事，不执行 DDL")
    p.add_argument("--no-lease", action="store_true",
                   help="跳过结构变更互斥（仅单机直跑 / 已确认无并发；多副本请留着）。"
                        "关掉后 ensure/drop 连库层咨询锁都没有（那只盖迁移与转换）")
    p.add_argument("--json", action="store_true", help="输出 JSON（供调度器采集）")
    args = p.parse_args(argv)

    from data_dog.storage import partitioning

    # 只有"真的会改结构"的动作参与互斥：--check / --dry-run 是只读的，不该抢锁。
    mutating = bool(not args.dry_run and (args.convert or args.ensure_months or args.drop_before))
    out: dict[str, Any] = {"table": partitioning.PARTITION_TABLE, "actions": [], "error": ""}
    lease_blocker = ""

    try:
        # 只读动作（--check / --dry-run）直接 lease=False：**连共享状态都不碰**。
        with ddl_guard(lease=False if (args.no_lease or not mutating) else None) as blocker:
            if blocker is not None:
                lease_blocker = blocker  # 有人正在做结构变更 → 本次一个 DDL 都不执行
            with session_scope(system=True) as s:
                _run(args, s, out, lease_blocker, partitioning)
    except partitioning.ConversionRefused as exc:
        # 静默窗口/确认令牌等前置条件在库层抛出：归一到"不该执行"（退出码 2），
        # 并带上机器可读原因（reason/last_write_age_s/expect…），不靠解析异常文本。
        out["error"] = "前置条件不满足——转换被拒绝"
        out["refused"] = dict(exc.details)
        out["refused"]["blockers"] = list(exc.blockers)
    except partitioning.ConversionBusy as exc:
        # 库层咨询锁被别人持有：这轮不该我干（3），不是错误。
        record_skip("convert_busy", metrics.partition_ddl_skipped_total)
        out["skipped"] = "convert_busy"
        out["busy"] = str(exc)
    except partitioning.PartitioningBusy as exc:
        # ensure/drop 的库层兜底锁被占（只在 --no-lease 时可能）：语义同 convert_busy。
        record_skip("ddl_busy", metrics.partition_ddl_skipped_total)
        out["skipped"] = "ddl_busy"
        out["busy"] = str(exc)

    if out.get("error"):
        exit_code = 2
    elif lease_blocker or out.get("skipped"):
        exit_code = 3
    else:
        exit_code = 0

    if lease_blocker:
        record_skip("lease_held", metrics.partition_ddl_skipped_total)
        out["skipped"] = "lease_held"
        out["lease_owner"] = lease_blocker

    if args.json:
        import json
        print(json.dumps(out, ensure_ascii=False))
        return exit_code

    _print_report(out, args)
    return exit_code


def session_scope(*, system: bool):
    """延迟导入，保证脚本被 import 时不连带加载数据库依赖（tests 直接加载本模块）。"""
    from data_dog.storage.db import session_scope as _scope
    return _scope(system=system)


def _run(args, s: Any, out: dict[str, Any], lease_blocker: str, partitioning: Any) -> None:
    """把三个子动作跑完；"不该执行"以 out[error] / out[skipped] 表达，由 main 归一到退出码。"""
    from sqlalchemy import text

    out.update(partitioning.describe(s, partitioning.PARTITION_TABLE))

    if not out["postgres"] and (args.ensure_months or args.drop_before):
        out["error"] = ("当前库不是 PostgreSQL：分区是 PG 能力，SQLite 走原分批 DELETE"
                        "（无需分区维护）")
        return

    if args.convert:
        _run_convert(args, s, out, lease_blocker, partitioning, text)

    if args.ensure_months:
        planned = planned_partitions(args.ensure_months)
        if out["postgres"] and not out["partitioned"]:
            out["error"] = ("目标还不是分区表——先用 --convert 转换（--convert --dry-run 看计划；"
                            "转换是停机窗口操作）")
            return
        if out["postgres"]:
            if args.dry_run:
                out["actions"].append({"ensure": planned, "executed": False})
            elif not lease_blocker:
                done = partitioning.ensure_partitions(
                    s, partitioning.PARTITION_TABLE, months=args.ensure_months,
                    lease_off=args.no_lease)
                out["actions"].append({"ensure": done, "executed": True})

    if args.drop_before:
        cutoff = parse_cutoff(args.drop_before)
        candidates = [n for n in out["partitions"]
                      if (partitioning.partition_end(n) or float("inf")) <= cutoff]
        if out["postgres"]:
            if args.dry_run:
                out["actions"].append({"drop": candidates, "executed": False})
            elif not lease_blocker:
                done = partitioning.drop_partitions_before(
                    s, cutoff, partitioning.PARTITION_TABLE, lease_off=args.no_lease)
                out["actions"].append({"drop": done, "executed": True})


def _run_convert(args, s: Any, out: dict[str, Any], lease_blocker: str,
                 partitioning: Any, text: Any) -> None:
    """--convert 一条路：确认门 → 计划/阻塞项 → dry-run → 互斥 → 库层锁 → 静默窗口。"""
    if not out["postgres"]:
        out["error"] = "当前库不是 PostgreSQL：分区是 PG 能力，无需转换"
        return

    plan = partitioning.plan_conversion(s, partitioning.PARTITION_TABLE)
    out["plan"] = plan
    if plan["blockers"]:
        out["error"] = "前置检查未通过——转换被拒绝（见阻塞项）"
        return

    if args.dry_run:
        # 静默窗口是**只读探测**：dry-run 不拒绝、也不抢锁，只把信号报出来供人判断。
        if args.require_quiet is not None:
            try:
                out["quiet"] = partitioning.check_convert_quiet(
                    s, partitioning.PARTITION_TABLE, seconds=args.require_quiet)
                out["quiet_ok"] = True
            except partitioning.ConversionRefused as exc:
                out["quiet"] = exc.details
                out["quiet_ok"] = False
        out["actions"].append({"convert": plan["actions"], "executed": False})
        return

    if lease_blocker:
        return                               # 被互斥挡住：一个 DDL 都不执行（结果里报 skipped）

    # 确认门：在拿锁/动结构之前就拒，不浪费一次租约，也不让"手滑转错表"变成现实。
    partitioning.check_convert_confirm(partitioning.PARTITION_TABLE, args.confirm_table)

    try:
        res = partitioning.convert_to_partitioned(
            s, partitioning.PARTITION_TABLE, keep_old=args.keep_old,
            confirm=args.confirm_table, require_quiet=args.require_quiet,
            verify=not args.no_verify)
    except partitioning.ConversionBusy as exc:
        # 库层锁忙：与租约同语义（这轮不该我），不报 error，交由 main 归到退出码 3。
        s.rollback()
        out["skipped"] = "convert_busy"
        out["busy"] = str(exc)
        return
    s.commit()
    s.execute(text("ANALYZE " + partitioning.PARTITION_TABLE))
    s.commit()
    out["actions"].append({"convert": res["notes"], "executed": True})
    out["partitions"] = res["partitions"]
    out["partition_count"] = len(res["partitions"])
    out["partitioned"] = True
    out["verified"] = res.get("verified", False)
    out["rows"] = res.get("rows")
    if res.get("quiet") is not None:
        out["quiet"] = res["quiet"]


def _print_report(out: dict[str, Any], args) -> None:
    """人类可读报告（--json 时不走这里）。"""
    print(f"===== {out['table']} 分区状态（dialect={out['dialect']}）=====")
    if not out["postgres"]:
        print("  非 PostgreSQL：分区不可用（SQLite 走分批 DELETE，无需维护）")
    elif not out["partitioned"]:
        print("  还不是分区表（普通表）——转换见 docs/storage.md 的 runbook")
    else:
        print(f"  分区表，共 {out['partition_count']} 个分区："
              + (", ".join(out["partitions"]) or "无"))
    if args.dry_run:
        print("  （DRY-RUN：未执行任何 DDL）")
    for act in out["actions"]:
        for kind, names in act.items():
            if kind == "executed":
                continue
            print(f"  {kind}: " + (", ".join(names) or "无"))
    if out.get("rows") is not None:
        print(f"  拷贝核对通过：{out['rows']} 行"
              + ("，内容指纹已比对" if out.get("verified") else "（仅行数，--no-verify）"))
    if out.get("quiet") is not None:
        q = out["quiet"]
        age = q.get("last_write_age_s")
        age_txt = "空表（无写入）" if not q.get("present") else f"{age:.1f}s 前"
        print(f"  静默窗口信号：最近写入 = {age_txt}"
              + ("" if out.get("quiet_ok", True) else "  [未达要求]"))
    if out.get("blockers"):
        print("  转换阻塞项（不解决就不能把这张表转成分区表）：")
        for item in out["blockers"]:
            print(f"    - {item}")
    if out.get("skipped"):
        why = ("结构变更互斥租约被 " + out.get("lease_owner", "?") + " 持有"
               if out["skipped"] == "lease_held" else "库层咨询锁显示另一个转换在跑")
        print(f"  [跳过] {why}——本次没执行任何 DDL（不是失败，下一轮再来）")
        if out["skipped"] == "lease_held":
            print("         确认无并发要单机直跑：--no-lease 或 DD_PARTITION_DDL_LEASE=0")
    if out.get("refused"):
        for item in out["refused"].get("blockers", []):
            print(f"    - {item}")
    if out["error"]:
        print(f"  [错误] {out['error']}")


if __name__ == "__main__":
    sys.exit(main())
