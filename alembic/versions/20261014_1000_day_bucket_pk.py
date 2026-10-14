"""遥测事件主键加「摄取日桶」created_day（A9 分区的前置条件）

为什么
----
`telemetry_events` 想按时间分区（A9：整段 `DROP PARTITION` 替代逐行 DELETE），但 PG 有硬规则：
**分区表的唯一约束必须包含全部分区键**。该表主键是 `(tenant_id, event_id)`，与 `created_at` 无关，
所以"按 `created_at` 范围分区"在 PG 上直接被拒——真 PG 实测报
`FeatureNotSupported: unique constraint on partitioned table must include all partitioning columns`
（见 docs/storage.md §8.2）。

`created_day = floor(created_at / 86400)`（**摄取日**，epoch 秒的日粒度）是解法：

- 它由**同一次写入的 `created_at`** 派生 → 稳定键 → 同日重投递仍命中同一主键，**幂等保住**；
- 进主键后 `PARTITION BY RANGE (created_day)` 合法；
- 保留策略仍按**摄取时间**（不是事件时间）——晚到的历史事件落在今天的桶，不会被立刻清掉。

代价（**已知且已被接受**）
----------------------
**跨天重投递会插入第二行**（同一 `(tenant_id, event_id)` 落在不同日桶）。同日重试（绝大多数重试）
仍幂等。检测查询见 docs/storage.md §8.2 的 `count_cross_day_duplicates`。

方言
----
- PostgreSQL：`ADD COLUMN`（可空）→ 回填 → `SET NOT NULL` → 换主键（`ALTER`，不重写数据）；
- SQLite：走 batch 重建表（SQLite 不支持改主键），batch 会带上已存在的索引。

⚠️ 运维注意：回填是一条 `UPDATE`（大表上是一个长事务）。**有数据的大表**应先按 docs/storage.md
§8.2 的 runbook 处理（分批回填 + 停机窗口），不要在启动自动迁移里对亿级表执行本迁移。

downgrade 保留数据但**可能失败**：若已存在跨天重复行，旧主键 `(tenant_id, event_id)` 建不起来
——**失败是对的**（宁可报错，也不静默删掉用户数据）。要回退先按 §8.2 的检测清重复。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a9c1d2e3f4b5"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "telemetry_events"
_PK = "pk_telemetry_events"
_OLD_PK_COLS = ["tenant_id", "event_id"]
_NEW_PK_COLS = ["tenant_id", "event_id", "created_day"]


def _backfill_expr(dialect: str) -> str:
    """`floor(created_at / 86400)` 的可移植写法。

    不能用 `CAST(created_at / 86400 AS BIGINT)` 一把梭：PG 的 double→整型转换是**四舍五入**，
    正午之后的时刻会被推进次日（真错）。SQLite 的 `CAST` 是截断，但 SQLite 未必编了 `floor()`
    （`SQLITE_ENABLE_MATH_FUNCTIONS`），所以按方言各取安全写法。
    """
    if dialect == "postgresql":
        return "FLOOR(created_at / 86400)::bigint"
    return "CAST(created_at / 86400 AS INTEGER)"       # SQLite：正数截断 == floor


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    op.add_column(_TABLE, sa.Column("created_day", sa.BigInteger(), nullable=True))
    op.execute(sa.text(f"UPDATE {_TABLE} SET created_day = {_backfill_expr(dialect)} "
                       "WHERE created_day IS NULL"))
    if dialect == "postgresql":
        op.alter_column(_TABLE, "created_day", existing_type=sa.BigInteger(), nullable=False)
        op.drop_constraint(_PK, _TABLE, type_="primary")
        op.create_primary_key(_PK, _TABLE, _NEW_PK_COLS)
    else:
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            batch.alter_column("created_day", existing_type=sa.BigInteger(), nullable=False)
            batch.drop_constraint(_PK, type_="primary")
            batch.create_primary_key(_PK, _NEW_PK_COLS)


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.drop_constraint(_PK, _TABLE, type_="primary")
        op.create_primary_key(_PK, _TABLE, _OLD_PK_COLS)
        op.drop_column(_TABLE, "created_day")
    else:
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            batch.drop_constraint(_PK, type_="primary")
            batch.create_primary_key(_PK, _OLD_PK_COLS)
            batch.drop_column("created_day")