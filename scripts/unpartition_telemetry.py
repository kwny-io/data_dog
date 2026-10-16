"""把分区表**降回普通表**（A9 转换的恢复路径）。

    python scripts/unpartition_telemetry.py --check                     # 只读：能否退回、有没有阻塞项
    python scripts/unpartition_telemetry.py --unpartition --dry-run     # 看计划，不动结构
    python scripts/unpartition_telemetry.py --unpartition --confirm-table telemetry_events --require-quiet 30

为什么需要它（真实缺口）：`partition_telemetry.py --convert` 的默认路径（`keep_old=False`）会删掉
旧表，而此前**没有反向脚本**——转换一旦提交，想退回普通表只能人工 `INSERT SELECT + 改名`。
它与 `--keep-old` 互补：

- 转换**前**就想要后路：`--convert --keep-old`（旧表原封不动，代价是占一份空间）；
- 已经转换了才**要退**：本脚本（分区表 → 普通表，主键回到转换前的形状）。

前置条件（不满足就拒绝，退出码 2）：

- **没有跨天重复**：旧主键 `(tenant_id, event_id)` 建不起来——`--check` 会点名是哪几组；
- **确认令牌** `--confirm-table`：原样复述表名（与转换、租户清理同一规矩）；
- **静默窗口** `--require-quiet N`：最近一次写入必须早于 N 秒（`INSERT ... SELECT` 拿的是读快照，
  事务期间的并发写入不会进新表）。

退出码：`0` 完成 / `2` 前置条件不满足（不该执行）/ `3` 这轮不该我干（租约或咨询锁被别人持有）。
仍是**停机窗口**操作，执行前停止接入。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from data_dog.core import metrics
from data_dog.core.console import ensure_utf8_console
from data_dog.partition_ddl import ddl_guard, record_skip


def _pg_like(out: dict[str, Any]) -> bool:
    """按 describe 的形状判断"是不是 PG 形态"。

    为什么不直接用 `partitioning.is_postgres(session)`：单测会在 SQLite 上把 `describe`
    换成 PG 形状来覆盖互斥分支，那时按真库判断会得出"非 PG"而走错路径——形状的唯一来源是
    `describe`（与 partition_telemetry 同一约定）。
    """
    return bool(out.get("postgres")) or str(out.get("dialect")) == "postgresql"


def session_scope(*, system: bool):
    """延迟导入，保证脚本被 import 时不连带加载数据库依赖（tests 直接加载本模块）。"""
    from data_dog.storage.db import session_scope as _scope

    return _scope(system=system)


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_console()
    p = argparse.ArgumentParser(description="telemetry_events 分区 → 普通表（A9 恢复路径）")
    p.add_argument("--check", action="store_true", help="只读：能否退回、有哪些阻塞项")
    p.add_argument("--unpartition", action="store_true", help="真的执行反向转换")
    p.add_argument("--confirm-table", default="", metavar="表名",
                   help="必须原样复述表名（不可逆动作的确认门）")
    p.add_argument("--require-quiet", type=float, default=None, metavar="秒",
                   help="要求最近一次写入早于 N 秒，否则拒绝")
    p.add_argument("--keep-partitioned", action="store_true",
                   help="保留原分区表为 <表>_old，而不是删掉")
    p.add_argument("--no-verify", action="store_true",
                   help="跳过反向转换后的整表内容指纹核对（只留行数核对）")
    p.add_argument("--dry-run", action="store_true", help="只打印将要做的事，不执行 DDL")
    p.add_argument("--no-lease", action="store_true",
                   help="不抢结构变更租约（单机确认无并发时用）")
    p.add_argument("--json", action="store_true", help="输出 JSON（供调度器采集）")
    args = p.parse_args(argv)

    from data_dog.storage import partitioning

    table = partitioning.PARTITION_TABLE
    mutating = bool(args.unpartition and not args.dry_run)
    out: dict[str, Any] = {"table": table, "actions": [], "error": "", "blockers": []}
    lease_blocker = ""

    try:
        with ddl_guard(lease=False if (args.no_lease or not mutating) else None) as blocker:
            if blocker is not None:
                lease_blocker = blocker
            with session_scope(system=True) as s:
                out.update(partitioning.describe(s, table))
                if not out["postgres"]:
                    out["error"] = "当前库不是 PostgreSQL：分区是 PG 能力，无需反向转换"
                elif not out["partitioned"]:
                    out["error"] = f"{table} 不是分区表，无需反向转换"
                elif _pg_like(out):
                    # 阻塞项要查 pg_class（只有 PG 有）；dry-run 只报告、不据此拒绝。
                    out["blockers"] = partitioning.unpartition_blockers(s, table)
                    out["old_primary_key"] = _derive_old_pk(s, partitioning, table)
                    if out["blockers"] and not args.dry_run:
                        out["error"] = "存在阻塞项——不能退回普通表（见 blockers）"
                    if args.dry_run:
                        _run(args, s, out, partitioning)      # dry-run：只把计划写进 actions
                    if args.unpartition and not args.dry_run and not lease_blocker:
                        _run(args, s, out, partitioning)
    except partitioning.ConversionRefused as exc:
        out["error"] = "前置条件不满足——反向转换被拒绝"
        out["refused"] = dict(exc.details)
        out["refused"]["blockers"] = list(exc.blockers)
    except partitioning.PartitioningBusy as exc:
        record_skip("unpartition_busy", metrics.partition_ddl_skipped_total)
        out["skipped"] = "unpartition_busy"
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
        print(json.dumps(out, ensure_ascii=False))
        return exit_code
    _print_report(out, args)
    return exit_code


def _derive_old_pk(s: Any, partitioning: Any, table: str) -> str:
    """派生的旧主键（去掉分区键列后剩下的），只读展示用。

    非 PG 直接返回空串：这是**能力边界**不是错误——库层 unpartition 同样先判 is_postgres。
    """
    from sqlalchemy import text

    if not partitioning.is_postgres(s):
        return ""
    cols = [r[0] for r in s.execute(text(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = to_regclass(:t) AND i.indisprimary ORDER BY a.attnum"),
        {"t": table})]
    rest = [c for c in cols if c != partitioning.PARTITION_KEY]
    return "(" + ", ".join(rest) + ")" if rest else ""


def _run(args, s: Any, out: dict[str, Any], partitioning: Any) -> None:
    """确认门 → 计划（dry-run 到此为止）→ 真执行。

    确认门在 CLI 这层**也**校验一次（库层还会再校验）：不可逆动作前面，宁可两道。
    """
    table = partitioning.PARTITION_TABLE
    out["actions"] = [{"unpartition": [
        f"卸下 {table} 的旧主键（连带子分区约束）",
        f"建普通表、按旧主键 {out.get('old_primary_key') or '(推导)'} 加主键、重建规范名索引",
        "复制 RLS 开关/策略/非属主授权 → 核对内容（行数 + 整表指纹）→ 改名切换",
    ], "executed": False}]
    if args.dry_run:
        return
    partitioning.check_unpartition_confirm(table, args.confirm_table)
    res = partitioning.unpartition(
        s, table, old_primary_key=out.get("old_primary_key") or "",
        keep_partitioned=args.keep_partitioned, confirm=args.confirm_table,
        require_quiet=args.require_quiet, verify=not args.no_verify)
    s.commit()
    out["actions"] = [{"unpartition": res["notes"], "executed": True}]
    out["partitioned"] = False
    out["partitions"] = []
    out["partition_count"] = 0
    out["rows"] = res["rows"]
    out["verified"] = res.get("verified", False)
    if res.get("quiet") is not None:
        out["quiet"] = res["quiet"]
        out["quiet_ok"] = True


def _print_report(out: dict[str, Any], args) -> None:
    print(f"===== {out['table']} 恢复为普通表（dialect={out.get('dialect')}）=====")
    if not out.get("postgres"):
        print("  非 PostgreSQL：分区不可用，无需恢复")
    elif not out.get("partitioned"):
        print("  当前不是分区表（普通表）——无需恢复")
    else:
        print(f"  当前是分区表（{out.get('partition_count')} 个分区）")
    if args.dry_run:
        print("  （DRY-RUN：未执行任何 DDL）")
    for act in out["actions"]:
        for kind, names in act.items():
            if kind == "executed":
                continue
            print(f"  {kind}: ")
            for n in names:
                print(f"    - {n}")
    if out.get("rows") is not None:
        print(f"  拷贝核对通过：{out['rows']} 行"
              + ("，内容指纹已比对" if out.get("verified") else "（仅行数，--no-verify）"))
    if out.get("blockers"):
        print("  阻塞项（不解决就不能退回普通表）：")
        for item in out["blockers"]:
            print(f"    - {item}")
    if out.get("skipped"):
        print(f"  [跳过] {out['skipped']}——本次没执行任何 DDL（不是失败，下一轮再来）")
    if out.get("error"):
        print(f"  [错误] {out['error']}")


if __name__ == "__main__":
    sys.exit(main())
