"""add operator_registry + operator_rollout_event (P4-2 控制面)

算子工厂从"demo"变"产品"的持久化落点：算子能**版本化注册、签名、灰度上线、
按指标自动回滚**。两表：

- operator_registry — 每个 (tenant_id, operator_id, version) 一行，含状态、源、
  内容哈希 dsl_hash、签名、rollout、指标快照。
- operator_rollout_event — 状态转移**台账**（审计）：谁、何时、因何把某版本推入
  Canary 或回滚。独立表而非 JSON 数组，因为审计要跨算子按时间/操作人查询。

纯新增表，既有数据不受影响。可逆：downgrade 删表 + 撤 RLS。

命名 `_a1_` 前缀：与其他 P4 并行项（a2/a3…）各自从同一 head 分支，避免 alembic
撞号（见 docs/p4/README.md §集成点）。合并多条并行链时用 `alembic merge`。

RLS：与其余租户表一致启用 PostgreSQL 行级安全。SQLite 下整段 no-op（本地开发
仍是应用层过滤）。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'a2b3c4d5e6f7'
down_revision: str | None = 'b1c2d3e4f5a6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
        "operator_registry",
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("operator_id", sa.String(length=120), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False,
                  server_default="Registered"),
        sa.Column("dsl_source", sa.Text(), nullable=False, server_default=""),
        sa.Column("dsl_hash", sa.String(length=64), nullable=False,
                  server_default=""),
        sa.Column("signature", sa.JSON(), nullable=True),
        sa.Column("rollout", sa.JSON(), nullable=True),
        sa.Column("metrics_snapshot", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "operator_id", "version",
                                name="pk_operator_registry"),
    )
    with op.batch_alter_table("operator_registry", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_operator_registry_tenant_id"),
                              ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_operator_registry_state"),
                              ["state"], unique=False)
        batch_op.create_index(batch_op.f("ix_operator_registry_dsl_hash"),
                              ["dsl_hash"], unique=False)

    op.create_table(
        "operator_rollout_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("operator_id", sa.String(length=120), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=False),
        sa.Column("from_state", sa.String(length=30), nullable=True),
        sa.Column("to_state", sa.String(length=30), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("actor", sa.String(length=120), nullable=False,
                  server_default=""),
        sa.Column("at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_operator_rollout_event"),
    )
    with op.batch_alter_table("operator_rollout_event", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_operator_rollout_event_tenant_id"),
                              ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_operator_rollout_event_operator_id"),
                              ["operator_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_operator_rollout_event_version"),
                              ["version"], unique=False)

    if _is_postgres():
        _enable_rls("operator_registry")
        _enable_rls("operator_rollout_event")


def downgrade() -> None:
    if _is_postgres():
        _disable_rls("operator_rollout_event")
        _disable_rls("operator_registry")
    with op.batch_alter_table("operator_rollout_event", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_operator_rollout_event_version"))
        batch_op.drop_index(batch_op.f("ix_operator_rollout_event_operator_id"))
        batch_op.drop_index(batch_op.f("ix_operator_rollout_event_tenant_id"))
    op.drop_table("operator_rollout_event")
    with op.batch_alter_table("operator_registry", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_operator_registry_dsl_hash"))
        batch_op.drop_index(batch_op.f("ix_operator_registry_state"))
        batch_op.drop_index(batch_op.f("ix_operator_registry_tenant_id"))
    op.drop_table("operator_registry")
