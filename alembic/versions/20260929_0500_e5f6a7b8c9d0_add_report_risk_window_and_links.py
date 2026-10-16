"""add reports.counter_evidence_refs/risk_window/linked_candidate_refs

调查报告 dataclass（`schemas/events.py::InvestigationReport`）早已有这三个字段、
`AgentLoop` 也会填（`risk_window` 建议检查时限、`linked_candidate_refs` 多事件关联），
但 `reports` 表无对应列 → `save_report` 落库时**静默丢弃**，前端 `GET /v1/reports`
读到的报告缺这几项（风险窗口、反证引用、关联候选全丢）。

本迁移补列。均为可空 / 默认空，纯新增，既有行不受影响。

可逆：downgrade 删三列。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'e5f6a7b8c9d0'
down_revision: str | None = 'd4e5f6a7b8c9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reports", schema=None) as batch_op:
        batch_op.add_column(sa.Column("counter_evidence_refs", sa.JSON(),
                                      nullable=True))
        batch_op.add_column(sa.Column("risk_window", sa.String(length=120),
                                      nullable=True))
        batch_op.add_column(sa.Column("linked_candidate_refs", sa.JSON(),
                                      nullable=True))
    # 回填既有行：两个 list 列的语义是"空列表"而非 NULL（新建行由模型 default=list
    # 填 []，旧行迁移后是 NULL → 读出来会是 None，破坏 list 契约）。risk_window 的
    # NULL 语义正确（"未给出风险窗口"），不回填。
    op.execute("UPDATE reports SET counter_evidence_refs = '[]' "
               "WHERE counter_evidence_refs IS NULL")
    op.execute("UPDATE reports SET linked_candidate_refs = '[]' "
               "WHERE linked_candidate_refs IS NULL")


def downgrade() -> None:
    with op.batch_alter_table("reports", schema=None) as batch_op:
        batch_op.drop_column("linked_candidate_refs")
        batch_op.drop_column("risk_window")
        batch_op.drop_column("counter_evidence_refs")
