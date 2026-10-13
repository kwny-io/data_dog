"""add investigation_run_log（调查运行日志：ReAct 轨迹 + 失败上下文）

背景
----
「技术人员事后调出运行日志发现问题」此前**做不到**：
- ReAct 轨迹只在 `POST /v1/investigations` 的**同步响应**里返回，异步 worker 路径直接丢弃；
- job 失败只留一个 `error_code` 枚举（`timeout` / `execution_failed`…），没有"第几步、哪次
  调用、provider 说了什么"；
- 调查报告（`reports`）只有**结论**，没有过程；而进程 stdout 的结构化日志要靠外部采集
  （Loki/ELK），产品内没有取回通道。

本表存什么
----------
一行 = 一次调查的**执行记录**：`steps`（每步工具名/入参/裁剪后的结果）、`usage`（token/耗时/
成本）、终态 `status` 与失败上下文（`error_code` + `error_detail`）。**失败也落**——这正是
最需要排查的一类。

设计要点
--------
- 主键 `(tenant_id, run_id)`：`run_id` = 异步的 `job_id` 或同步的 `investigation_id`；
  两种 id 跨租户可能重名，故 `tenant_id` 进主键（与其余租户表一致）。
- **启用 RLS** 且策略自带系统扫描分支（本表建于 `b7c8d9e0f1a2` 之后，只写"仅租户"谓词会让
  运维/保留清理**零行可见**——那类静默失效已经踩过一次）。
- 索引：`candidate_id`（按候选翻它的历次运行）、`status`、`created_at`（保留清理）。

可逆：downgrade 删表 + 策略。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'e1f2a3b4c5d6'
down_revision: str | None = 'd0e1f2a3b4c5'
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
        "investigation_run_log",
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("run_id", sa.String(length=120), nullable=False),
        sa.Column("candidate_id", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="sync"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("report_id", sa.String(length=120), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("step_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("steps", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("usage", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "run_id", name="pk_investigation_run_log"),
    )
    with op.batch_alter_table("investigation_run_log", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_investigation_run_log_tenant_id"),
                              ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_investigation_run_log_candidate_id"),
                              ["candidate_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_investigation_run_log_status"),
                              ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_investigation_run_log_created_at"),
                              ["created_at"], unique=False)

    if _is_postgres():
        op.execute("ALTER TABLE investigation_run_log ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE investigation_run_log FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON investigation_run_log "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    if _is_postgres():
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON investigation_run_log")
        op.execute("ALTER TABLE investigation_run_log NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE investigation_run_log DISABLE ROW LEVEL SECURITY")
    with op.batch_alter_table("investigation_run_log", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_investigation_run_log_created_at"))
        batch_op.drop_index(batch_op.f("ix_investigation_run_log_status"))
        batch_op.drop_index(batch_op.f("ix_investigation_run_log_candidate_id"))
        batch_op.drop_index(batch_op.f("ix_investigation_run_log_tenant_id"))
    op.drop_table("investigation_run_log")
