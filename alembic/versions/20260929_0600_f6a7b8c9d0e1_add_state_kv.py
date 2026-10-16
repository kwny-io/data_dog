"""add state_kv (跨副本共享状态)

水平扩展要求有状态算子（限流桶、候选分组）外置到所有副本都能读到的地方。
`state_kv` 是 PostgreSQL 后端 `StateStore.mutate` 的载体：以
`SELECT ... FOR UPDATE` 行锁做原子 read-modify-write。

纯新增表，既有数据不受影响。可逆：downgrade 删表。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'f6a7b8c9d0e1'
down_revision: str | None = 'e5f6a7b8c9d0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "state_kv",
        sa.Column("key", sa.String(length=300), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("state_kv")
