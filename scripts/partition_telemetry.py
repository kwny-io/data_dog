"""`telemetry_events` 分区维护 CLI（架构评审 A9）。

三个子用途（都要求 PostgreSQL；非 PG 会**说清楚并退出**，而不是假装成功）：

    python scripts/partition_telemetry.py --check                 # 只读：是否分区 / 有哪些分区
    python scripts/partition_telemetry.py --ensure-months 3       # 建"本月起未来 3 个月"分区（幂等）
    python scripts/partition_telemetry.py --drop-before 2026-08   # 删"整段早于 2026-08"的分区
    python scripts/partition_telemetry.py --convert --dry-run     # 存量普通表 → 分区表：先看计划
    python scripts/partition_telemetry.py --convert               # 执行（**停机窗口**，见下）

`--convert` 是**停机窗口**操作：单事务里建新分区父表 → 拷数据 → 复制 RLS/策略/授权 → 核对行数 →
改名切换 → 删旧表。失败整体回滚，旧表不动。**执行前停止接入**（事务里拷的是读快照，期间的
并发写入不会进新表）。前置检查不通过（`--check` 会列阻塞项）时**拒绝执行**、退出码 2。

为什么不塞进 `retention`：分区维护是**结构变更**（DDL），保留清理是**删数据**。把 DDL 和周期
删除混进同一个定时任务，出问题时没法只暂停其中一个。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any

from data_dog.core.console import ensure_utf8_console


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
    """"本月起未来 N 个月"的分区名（与 `ensure_partitions` 同口径，用于 --dry-run）。"""
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
    p.add_argument("--dry-run", action="store_true", help="只打印将要做的事，不执行 DDL")
    p.add_argument("--json", action="store_true", help="输出 JSON（供调度器采集）")
    args = p.parse_args(argv)

    from sqlalchemy import text

    from data_dog.storage import partitioning
    from data_dog.storage.db import session_scope

    exit_code = 0
    with session_scope(system=True) as s:
        info = partitioning.describe(s, partitioning.PARTITION_TABLE)
        out: dict[str, Any] = {"table": partitioning.PARTITION_TABLE, **info,
                               "actions": [], "error": ""}

        if not info["postgres"] and (args.ensure_months or args.drop_before):
            out["error"] = ("当前库不是 PostgreSQL：分区是 PG 能力，SQLite 走原分批 DELETE"
                            "（无需分区维护）")
            exit_code = 2

        if args.convert:
            if not info["postgres"]:
                out["error"] = "当前库不是 PostgreSQL：分区是 PG 能力，无需转换"
                exit_code = 2
            else:
                plan = partitioning.plan_conversion(s, partitioning.PARTITION_TABLE)
                out["plan"] = plan
                if plan["blockers"]:
                    out["error"] = "前置检查未通过——转换被拒绝（见阻塞项）"
                    exit_code = 2
                elif args.dry_run:
                    out["actions"].append({"convert": plan["actions"], "executed": False})
                else:
                    res = partitioning.convert_to_partitioned(
                        s, partitioning.PARTITION_TABLE, keep_old=args.keep_old)
                    s.commit()
                    s.execute(text("ANALYZE " + partitioning.PARTITION_TABLE))
                    s.commit()
                    out["actions"].append({"convert": res["notes"], "executed": True})
                    out["partitions"] = res["partitions"]
                    out["partition_count"] = len(res["partitions"])
                    out["partitioned"] = True

        if args.ensure_months:
            planned = planned_partitions(args.ensure_months)
            if info["postgres"] and not info["partitioned"]:
                out["error"] = ("目标还不是分区表——先用 --convert 转换（--convert --dry-run 看计划；"
                                "转换是停机窗口操作）")
                exit_code = 2
            elif info["postgres"]:
                if args.dry_run:
                    out["actions"].append({"ensure": planned, "executed": False})
                else:
                    done = partitioning.ensure_partitions(
                        s, partitioning.PARTITION_TABLE, months=args.ensure_months)
                    out["actions"].append({"ensure": done, "executed": True})

        if args.drop_before:
            cutoff = parse_cutoff(args.drop_before)
            candidates = [n for n in info["partitions"]
                          if (partitioning.partition_end(n) or float("inf")) <= cutoff]
            if info["postgres"]:
                if args.dry_run:
                    out["actions"].append({"drop": candidates, "executed": False})
                else:
                    done = partitioning.drop_partitions_before(
                        s, cutoff, partitioning.PARTITION_TABLE)
                    out["actions"].append({"drop": done, "executed": True})

    if args.json:
        import json
        print(json.dumps(out, ensure_ascii=False))
        return exit_code

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
    if out.get("blockers"):
        print("  转换阻塞项（不解决就不能把这张表转成分区表）：")
        for item in out["blockers"]:
            print(f"    - {item}")
    if out["error"]:
        print(f"  [错误] {out['error']}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
