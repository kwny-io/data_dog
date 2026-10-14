"""`telemetry_events` 分区维护 CLI（架构评审 A9）。

三个子用途（都要求 PostgreSQL；非 PG 会**说清楚并退出**，而不是假装成功）：

    python scripts/partition_telemetry.py --check                 # 只读：是否分区 / 有哪些分区
    python scripts/partition_telemetry.py --ensure-months 3       # 建"本月起未来 3 个月"分区（幂等）
    python scripts/partition_telemetry.py --drop-before 2026-08   # 删"整段早于 2026-08"的分区

**不做**的事（边界）：不把既有普通表转成分区表。那在 PG 上要建新父表 + 拷数据 + 重建索引/
外键 + 改名切换，是**需要停机窗口**的迁移，见 `docs/storage.md` 的 runbook（人工执行、可回滚）。

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
    p.add_argument("--dry-run", action="store_true", help="只打印将要做的事，不执行 DDL")
    p.add_argument("--json", action="store_true", help="输出 JSON（供调度器采集）")
    args = p.parse_args(argv)

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

        if args.ensure_months:
            planned = planned_partitions(args.ensure_months)
            if info["postgres"] and not info["partitioned"]:
                out["error"] = ("目标还不是分区表——先按 docs/storage.md 的 runbook 转换；"
                                "本脚本不自动改表结构")
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
    if out["error"]:
        print(f"  [错误] {out['error']}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
