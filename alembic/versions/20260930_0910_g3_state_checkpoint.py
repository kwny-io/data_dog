"""add state_checkpoint (G3 StateStore 检查点/恢复)

状态已外置到 `state_kv`（可跨副本共享），但进程重启或状态淘汰后**无法确认
"上次算到哪"**。本表存**周期快照**（状态词典的序列化副本），供恢复与对账。

- 不是流处理器精确一次 checkpoint——快照是**周期**的，两次快照之间重启会丢
  这段增量（RPO = 间隔）。语义边界见 `state/checkpoint.py`。
- payload 含租户维度状态，属**敏感数据**（存储加密/访问控制为运营职责）。

纯新增表，既有数据不受影响。可逆：downgrade 删表。
本表**不含租户列**（快照按 prefix 自含租户维度、跨租户统一管理），故**不启用 RLS**
——与 `state_kv` 一致（状态载体按 key 自含维度，为全局运维资源）。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'c1d2e3f4a5b6'
down_revision: str | None = 'b1c2d3e4f5a6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "state_checkpoint",
        sa.Column("checkpoint_id", sa.String(length=80), nullable=False),
        sa.Column("prefix", sa.String(length=300), nullable=False,
                  server_default=""),
        sa.Column("n_keys", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("checkpoint_id", name="pk_state_checkpoint"),
    )
    with op.batch_alter_table("state_checkpoint", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_state_checkpoint_created_at"),
                              ["created_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("state_checkpoint", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_state_checkpoint_created_at"))
    op.drop_table("state_checkpoint")
