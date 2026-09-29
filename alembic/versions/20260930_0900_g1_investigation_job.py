"""add investigation_job (G1 调查端点异步化)

`POST /v1/investigations` 此前是**同步**的：整个 HTTP 请求阻塞在一次可能
10–60s 的 LLM 调查上，占满 worker，端到端水平扩展卡在这一处。本表是异步化的
**执行状态载体**：

- job（本表）— 一次调查的执行状态（pending/running/succeeded/failed/cancelled），短命。
- report（既有 reports 表）— 调查的产物，长期。
完成时 job.report_id 指向 reports.investigation_id。

纯新增表，既有数据不受影响。可逆：downgrade 删表 + 撤 RLS。

RLS：与其余租户表一样启用 PostgreSQL 行级安全（策略见既有 RLS 迁移）。
SQLite 下整段是 no-op（本地开发仍是应用层过滤）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PREDICATE = "tenant_id = current_setting('app.tenant_id', true)"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "investigation_job",
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("job_id", sa.String(length=120), nullable=False),
        sa.Column("candidate_id", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="pending"),
        sa.Column("report_id", sa.String(length=120), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("worker_id", sa.String(length=120), nullable=False,
                  server_default=""),
        sa.Column("submitted_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "job_id", name="pk_investigation_job"),
    )
    with op.batch_alter_table("investigation_job", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_investigation_job_tenant_id"),
                              ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_investigation_job_candidate_id"),
                              ["candidate_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_investigation_job_status"),
                              ["status"], unique=False)

    # RLS：与其余租户表一致（数据库层强制隔离）。SQLite 无 RLS → 跳过。
    if _is_postgres():
        op.execute("ALTER TABLE investigation_job ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE investigation_job FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON investigation_job "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    if _is_postgres():
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON investigation_job")
        op.execute("ALTER TABLE investigation_job NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE investigation_job DISABLE ROW LEVEL SECURITY")
    with op.batch_alter_table("investigation_job", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_investigation_job_status"))
        batch_op.drop_index(batch_op.f("ix_investigation_job_candidate_id"))
        batch_op.drop_index(batch_op.f("ix_investigation_job_tenant_id"))
    op.drop_table("investigation_job")
