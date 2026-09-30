"""add change_records (G7 真实变更管理源)

变更系统（CMDB/发布系统）把"变了什么"推送到 `POST /v1/changes/ingest` 落此表，
Agent 的 `query_recent_changes` 查库并**真按 since 过滤**（此前 since 被静默忽略）。

- 主键 (tenant_id, change_id)：change_id 由来源系统给出，跨租户可能重名。
- `raw` 存来源特有字段，对 Agent 返回时展开，保持与内置表一致的形状。
- **启用 RLS**：变更记录含租户维度，与其它租户表一致（跨租户不可见）。

纯新增表，既有数据不受影响。可逆：downgrade 删表 + 策略。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'd7e8f9a0b1c2'
down_revision: str | None = 'c1d2e3f4a5b6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREDICATE = "tenant_id = current_setting('app.tenant_id', true)"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "change_records",
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("change_id", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("target", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("applied_at", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("from_version", sa.String(length=80), nullable=False,
                  server_default=""),
        sa.Column("to_version", sa.String(length=80), nullable=False,
                  server_default=""),
        sa.Column("actor", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("raw", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "change_id",
                                name="pk_change_records"),
    )
    with op.batch_alter_table("change_records", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_change_records_tenant_id"),
                              ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_change_records_target"),
                              ["target"], unique=False)
        batch_op.create_index(batch_op.f("ix_change_records_applied_at"),
                              ["applied_at"], unique=False)

    if _is_postgres():
        op.execute("ALTER TABLE change_records ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE change_records FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON change_records "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    if _is_postgres():
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON change_records")
        op.execute("ALTER TABLE change_records NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE change_records DISABLE ROW LEVEL SECURITY")
    with op.batch_alter_table("change_records", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_change_records_applied_at"))
        batch_op.drop_index(batch_op.f("ix_change_records_target"))
        batch_op.drop_index(batch_op.f("ix_change_records_tenant_id"))
    op.drop_table("change_records")
