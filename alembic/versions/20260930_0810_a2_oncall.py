"""add oncall_rota + alert_delivery (P4-3 通知/值班/升级 SLA)

补上告警闭环的最后一公里：**真人能收到、没人理会上自动升级**。两表：

- oncall_rota    — 值班表：谁在什么时段、按领域与级别值班。
- alert_delivery — 投递台账：每次投递一行，兼作**升级计时**（ack_deadline 超时未
  确认 → sweeper 升到下一级，写新行）。delivery_id = hash(event_id, level) 保证
  同告警同级别只投一次。

纯新增表，既有数据不受影响。可逆：downgrade 删表 + 撤 RLS。

命名 `_a2_` 前缀（见 docs/p4/README.md §集成点）：与其他 P4 并行项各自从同一 head
分支，避免 alembic 撞号。本迁移接在合并后的单一 head `e8f9a0b1c2d3` 之后。

RLS：与其余租户表一致启用 PostgreSQL 行级安全（`alert_delivery` 的升级扫描虽跨
租户读，但每条命中行在**逐租户会话**里处理，RLS 按 tenant_id 放行）。SQLite 下整段
no-op（本地开发仍是应用层过滤）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, None] = 'e8f9a0b1c2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PREDICATE = "tenant_id = current_setting('app.tenant_id', true)"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def _disable_rls(table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    op.create_table(
        "oncall_rota",
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("rota_id", sa.String(length=80), nullable=False),
        sa.Column("domain", sa.String(length=120), nullable=False,
                  server_default="*"),
        sa.Column("level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("assignee", sa.String(length=120), nullable=False,
                  server_default=""),
        sa.Column("start_at", sa.Float(), nullable=False),
        sa.Column("end_at", sa.Float(), nullable=False),
        sa.Column("channel", sa.String(length=40), nullable=False,
                  server_default="webhook"),
        sa.Column("target", sa.String(length=400), nullable=False,
                  server_default=""),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "rota_id", name="pk_oncall_rota"),
    )
    with op.batch_alter_table("oncall_rota", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_oncall_rota_tenant_id"),
                              ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_oncall_rota_domain"),
                              ["domain"], unique=False)

    op.create_table(
        "alert_delivery",
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("delivery_id", sa.String(length=80), nullable=False),
        sa.Column("event_id", sa.String(length=80), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("domain", sa.String(length=120), nullable=False,
                  server_default="*"),
        sa.Column("channel", sa.String(length=40), nullable=False,
                  server_default="webhook"),
        sa.Column("target", sa.String(length=400), nullable=False,
                  server_default=""),
        sa.Column("assignee", sa.String(length=120), nullable=False,
                  server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.Float(), nullable=True),
        sa.Column("acked_at", sa.Float(), nullable=True),
        sa.Column("ack_deadline", sa.Float(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "delivery_id",
                                name="pk_alert_delivery"),
    )
    with op.batch_alter_table("alert_delivery", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_alert_delivery_tenant_id"),
                              ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_alert_delivery_event_id"),
                              ["event_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_alert_delivery_status"),
                              ["status"], unique=False)

    if _is_postgres():
        _enable_rls("oncall_rota")
        _enable_rls("alert_delivery")


def downgrade() -> None:
    if _is_postgres():
        _disable_rls("alert_delivery")
        _disable_rls("oncall_rota")
    with op.batch_alter_table("alert_delivery", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_alert_delivery_status"))
        batch_op.drop_index(batch_op.f("ix_alert_delivery_event_id"))
        batch_op.drop_index(batch_op.f("ix_alert_delivery_tenant_id"))
    op.drop_table("alert_delivery")
    with op.batch_alter_table("oncall_rota", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_oncall_rota_domain"))
        batch_op.drop_index(batch_op.f("ix_oncall_rota_tenant_id"))
    op.drop_table("oncall_rota")
