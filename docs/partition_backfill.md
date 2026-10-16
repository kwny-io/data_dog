# 大表 `created_day` 分批回填（A9 分区迁移前置）

## 为什么有这一步

`telemetry_events` 要能"按时间整段 `DROP PARTITION`"，就得把分区键 `created_day`
（= `floor(created_at / 86400)`）加进主键（PG 的硬规则：分区表的唯一约束必须包含分区键）。
`alembic upgrade` 里的回填是**一条 `UPDATE`**：

- 亿级表上是一个**长事务**：WAL 峰值高、锁久（`ALTER` 换主键前一直被这个事务占着）、
- **失败全回滚**，只能重头再来，
- 期间表上别的 DDL 全被挡住。

所以顺序是：**先分批回填（本页）→ 再跑迁移**（那时迁移里的 `UPDATE` 命中 0 行，只剩改主键的 DDL）。

## 怎么做

```bash
# 1) 先看还差多少行（只读，不抢锁）
python scripts/backfill_created_day.py --check

# 2) 停机窗口内分批回填（幂等、可中断；中断后重跑即可）
python scripts/backfill_created_day.py --batch 50000

# 3) 确认没有 NULL 了，再跑迁移
python scripts/backfill_created_day.py --check      # pending 必须为 0
alembic upgrade head
```

- 连接串走 `DD_DATABASE_URL`（与其它运维脚本一致）。
- `--json` 给调度器采集；退出码：`0` 完成 / `2` 表或列不存在 / `3` 另一个人正在跑。
- 单机确认无并发可用 `--no-lock` 跳过 `partition:backfill` 会话级咨询锁。

## 它保证什么、不保证什么（如实写）

| 保证 | 不保证 |
|---|---|
| **幂等**：只碰 `created_day IS NULL` 的行，重跑安全；已填过的行不会被覆盖 | 不改结构（改主键仍由迁移负责） |
| **分批提交**：每批一次 `COMMIT`，中断后从中断处继续（不是"从零开始"） | 不是"一遍过"：批间有空隙，期间新写入各自自带 `created_day`，不影响结果 |
| **与迁移公式一致**：`_backfill_expr` 逐字相同，有测试钉住 | 不为你判断"现在能不能停服"——那是人的决定 |
| **并发保护**：`partition:backfill` 会话级锁（协作式，抢不到就退出码 3） | 协作式：绕过它直接跑不会坏数据，只是白干一遍 |

**回填窗口内量「跨天重复」注意口径**：`repository.count_cross_day_duplicates()` 只统计
`created_day IS NOT NULL` 的行，所以它在回填**没跑完**时给的是**下界**（重复行里还有没回填的那组
会被漏掉）。别拿它的返回值推断进度——进度单独用
`SELECT count(*) FROM telemetry_events WHERE created_day IS NULL` 核对。

## 真库验证（本机）

`tests/test_backfill_created_day.py`：离线腿 4 passed（SQLite `rowid` 分页、幂等、`commit_each`
语义、公式与迁移一致）；设 `DD_TEST_PG_URL` 后真库腿验证 PG `ctid` 分页 + 会话级锁互斥。

## 与其它文档的关系

- `docs/storage.md` §8.2 的 runbook 讲"为什么分区键必须是 `created_day`"与转换本身；本页只讲
  **大表怎么把回填跑完**。
- `alembic/versions/20261014_1000_day_bucket_pk.py` 的运维注意一节指向本页。
