"""大表分批回填 `telemetry_events.created_day`（A9 分区迁移的**前置**步骤）。

为什么单独一个脚本，而不是让 `alembic upgrade` 顺手做
-----------------------------------------------------------
迁移里的回填是**一条 `UPDATE`**：亿级表上就是一个长事务（WAL 峰值高、锁久、失败全回滚、
只能重头再来）。所以正确顺序是：

1. **先**用本脚本在**停机窗口**内分批回填（幂等、可中断重跑，只碰 `created_day IS NULL` 的行）；
2. **再**跑 `alembic upgrade`——那时迁移里的 `UPDATE` 命中 0 行，只剩改主键的 DDL。

用法
----
    python scripts/backfill_created_day.py --check          # 只读：还差多少行
    python scripts/backfill_created_day.py --batch 50000    # 真跑（可中断，重跑即可）
    python scripts/backfill_created_day.py --json           # 输出 JSON（给调度器采集）

退出码：`0` 完成 / `check` 成功；`2` 环境不对（表或列不存在、非 PG 且非 SQLite）；
`3` 另一个人正在跑（没抢到 `partition:backfill` 会话级咨询锁，单机可加 `--no-lock` 跳过）。

边界：并发写入**不冲突**（新写入自带 `created_day`）；锁是协作式的（绕过它直接跑不会坏数据，
只是白干一遍）；本脚本**不改结构**，改主键仍由迁移负责。
"""
from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from data_dog.storage.db import session_scope
from data_dog.storage.partitioning import PARTITION_TABLE, backfill_created_day
from data_dog.storage.pg_locks import PARTITION_BACKFILL_LOCK_KEY, advisory_session_lock


def _pending(session, table: str) -> int:
    return int(session.execute(text(f"SELECT count(*) FROM {table} WHERE created_day IS NULL")
                               ).scalar() or 0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="分批回填 created_day（A9 分区迁移前置）")
    ap.add_argument("--batch", type=int, default=50_000, metavar="N", help="每批行数（默认 50000）")
    ap.add_argument("--check", action="store_true", help="只读：打印还差多少行")
    ap.add_argument("--no-lock", action="store_true", help="不抢咨询锁（单机确认无并发时用）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)
    table = PARTITION_TABLE

    def say(text_out: str) -> None:
        if not args.json:
            print(text_out)

    with session_scope(system=True) as session:
        dialect = str(session.get_bind().dialect.name)
        try:
            pending = _pending(session, table)
        except ProgrammingError as e:
            print(f"✗ 表或列不存在（先跑迁移建列）：{type(e).__name__}", file=sys.stderr)
            return 2
        if args.check:
            payload = {"check": True, "dialect": dialect, "pending": pending, "table": table}
            print(json.dumps(payload, ensure_ascii=False) if args.json
                  else f"created_day 待回填：{pending} 行（{dialect}）")
            return 0
        lock = None if args.no_lock else advisory_session_lock(
            session, PARTITION_BACKFILL_LOCK_KEY, wait=False)
        if lock is None:
            rep = backfill_created_day(session, table, batch=args.batch, log=say)
        else:
            with lock as acquired:
                if not acquired:
                    print("skipped=lock_held（另一个人正在回填；改 --no-lock 可强制并行）",
                          file=sys.stderr)
                    return 3
                rep = backfill_created_day(session, table, batch=args.batch, log=say)
        payload = {"check": False, "table": table, "pending_before": pending, **rep}
        print(json.dumps(payload, ensure_ascii=False) if args.json
              else (f"完成：更新 {rep['updated']} 行 / {rep['batches']} 批（回填前待办 {pending}）"))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
