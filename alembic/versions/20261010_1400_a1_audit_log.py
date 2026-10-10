"""add audit_log（A1 安全审计面）

背景
----
`docs/slo.md` §6 一直声明"**审计记录保留 ≥ 1 年**"，但 `src/` 里**零实现**：
（grep `audit` 只命中依赖 SCA 与算子签名，没有"谁在何时对什么做了什么"的记录）。
这在企业采购/合规评审里是一票否决项，也让事故复盘只剩访问日志可翻——而访问日志
是**运维观测**（滚动、可能被采样/丢弃），不是**审计证据**（只增、可查、按年保留）。

本表存什么
----------
一条记录 = 一次**安全相关动作**及其结果：
- `auth.denied`：认证/授权失败（**含"声称某租户但密钥不对"这种冒充尝试**）；
- `ops.access`：运维面（`/v1/state/*`）被访问；
- 将来的 `tenant.purge` / `key.rotate` 等同表落库（⑥ 与 ② 会用到）。

设计要点
--------
- 主键 `(tenant_id, audit_id)`，与其余租户表一致；`audit_id` **唯一不幂等**
  （两次相同事件就是两条事实——审计不能去重）。
- **无租户上下文的系统事件**记在保留租户 `-`（未带 `X-Tenant-Id` 的拒绝、运维操作）。
- **启用 RLS**，且策略**自带系统扫描分支**：本表建于迁移 `b7c8d9e0f1a2` 之后，若只写
  旧的"仅租户"谓词，运维侧的跨租户审计查询会**零行可见**（正是 b7c8d9e0f1a2 修的那类
  静默失效）。故这里直接用带 `OR app.system_scan='on'` 的谓词。
- 索引：`ts`（按时间翻页/保留清理）、`action`、`outcome`（按类查）。

可逆：downgrade 删表 + 策略。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'c9d0e1f2a3b4'
down_revision: str | None = 'b7c8d9e0f1a2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 b7c8d9e0f1a2 之后的策略口径一致：租户分支 + **显式的系统扫描分支**
_PREDICATE = (
    "tenant_id = current_setting('app.tenant_id', true) "
    "OR coalesce(current_setting('app.system_scan', true), 'off') = 'on'"
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("audit_id", sa.String(length=120), nullable=False),
        sa.Column("ts", sa.Float(), nullable=False),
        sa.Column("actor", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("action", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("target", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("outcome", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("reason", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("request_id", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("trace_id", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("key_id", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("source_ip", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("detail", sa.JSON(), nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("tenant_id", "audit_id", name="pk_audit_log"),
    )
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_audit_log_tenant_id"),
                              ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_audit_log_ts"), ["ts"], unique=False)
        batch_op.create_index(batch_op.f("ix_audit_log_action"),
                              ["action"], unique=False)
        batch_op.create_index(batch_op.f("ix_audit_log_outcome"),
                              ["outcome"], unique=False)

    if _is_postgres():
        op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE audit_log FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON audit_log "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    if _is_postgres():
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON audit_log")
        op.execute("ALTER TABLE audit_log NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE audit_log DISABLE ROW LEVEL SECURITY")
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_audit_log_outcome"))
        batch_op.drop_index(batch_op.f("ix_audit_log_action"))
        batch_op.drop_index(batch_op.f("ix_audit_log_ts"))
        batch_op.drop_index(batch_op.f("ix_audit_log_tenant_id"))
    op.drop_table("audit_log")
