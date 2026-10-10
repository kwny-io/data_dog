"""add retention indexes（④ 数据保留清理作业的支撑索引）

背景
----
`docs/slo.md` §6 声明了"原始日志 7 天 / 规范化指标 90 天 / 审计 ≥ 1 年"，但此前
**没有任何清理路径**——声明只活在文档里。④ 落地了 `data_dog.retention`：按时间列
`< cutoff` **有界分批**删除，跨租户走 `session_scope(system=True)`（否则 RLS 下会
静默删 0 行），删除动作写 `retention.purge` 审计。

为什么清理一定要先有索引
-----------------------
清理的每一批都是 `WHERE 时间列 < :cutoff LIMIT n`。没有索引时，**每一批都是一次全表
扫描**：清理从"低频小事务"退化成"周期性把库拖慢"，而本项目恰恰已经因为"同步落库排队"
在 P99 上吃过亏（见 `docs/write_path_batching.md`）。索引是让"保留策略真的能长期跑"
的前提，不是可选优化。

本迁移只加索引、**不动数据**（可逆：downgrade 逐个删索引）。
`audit_log.ts` 的索引在 ① 的迁移（`c9d0e1f2a3b4`）里已建，这里不重复。
索引名与 `models.py` 里 `index=True` 的默认命名（`ix_<表>_<列>`）**保持一致**，
否则"迁移建的库"与"`create_all` 建的库"会各有一份不同名的索引。
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = 'd0e1f2a3b4c5'
down_revision: str | None = 'c9d0e1f2a3b4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (索引名, 表, 列) —— 与 models.py 的 index=True 一一对应
_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_telemetry_events_created_at", "telemetry_events", "created_at"),
    ("ix_candidates_created_at", "candidates", "created_at"),
    ("ix_reports_created_at", "reports", "created_at"),
    ("ix_alerts_created_at", "alerts", "created_at"),
    ("ix_alert_outbox_created_at", "alert_outbox", "created_at"),
    ("ix_alert_feedback_created_at", "alert_feedback", "created_at"),
    ("ix_alert_delivery_created_at", "alert_delivery", "created_at"),
    ("ix_investigation_job_finished_at", "investigation_job", "finished_at"),
)


def upgrade() -> None:
    for name, table, column in _INDEXES:
        op.create_index(name, table, [column], unique=False)


def downgrade() -> None:
    for name, table, _column in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
