"""JSON → JSONB（PostgreSQL）+ 租户维度复合索引

为什么
----
两件"声明与事实不符 / 查询走不上索引"的事：

1. `storage/models.py` 的 docstring 一直写"复杂字段用 JSON（**PostgreSQL 为 JSONB**）"，
   但 baseline 迁移用的是 `sa.JSON()`——在 PG 上落成 **json**，不是 jsonb：不能建 GIN 索引、
   不去重、每次取值都要重新解析。**文档说了 JSONB，那就得真的是 JSONB**（这条现在由
   `tests/test_project_meta.py::test_json_columns_are_jsonb_on_postgres` 钉住）。
2. 一批列表查询是"tenant 过滤 + created_at 排序"（candidates / reports / alerts /
   telemetry_events / investigation_job），而索引只有单列 tenant_id 与 created_at：
   PG 在两者之间只能选一个、排序还要另做。补 `(tenant_id, created_at[, tiebreaker])`。

覆盖范围
--------
`_JSON_COLUMNS` 是**穷举**的：测试会拿它与 SQLAlchemy 元数据比对，将来新增 JSON 列忘了改这里，
红灯（"清单写死然后与模型漂移"是本仓明确要避免的失效模式）。

方言
----
- PostgreSQL：ALTER 成 jsonb（`USING col::jsonb`）+ 建复合索引；
- SQLite：**不动类型**（无 jsonb），但复合索引照建（本地/SQLite 形态同样受益）。

可逆：downgrade 把 jsonb 退回 json 并删索引。
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (表, 列)：全部 JSON 列。与 storage/models.py 的元数据一一对应（有测试把关）。
_JSON_COLUMNS: tuple[tuple[str, str], ...] = (
        ('telemetry_events', 'metrics'),
        ('telemetry_events', 'status_codes'),
        ('candidates', 'rule_matches'),
        ('candidates', 'evidence'),
        ('candidates', 'evidence_refs'),
        ('reports', 'candidate_refs'),
        ('reports', 'hypotheses'),
        ('reports', 'counter_evidence_refs'),
        ('reports', 'missing_evidence'),
        ('reports', 'recommended_actions'),
        ('reports', 'linked_candidate_refs'),
        ('reports', 'model_provenance'),
        ('alerts', 'recommended_actions'),
        ('alerts', 'history'),
        ('alert_outbox', 'payload'),
        ('change_records', 'raw'),
        ('state_kv', 'value'),
        ('state_checkpoint', 'payload'),
        ('investigation_run_log', 'steps'),
        ('investigation_run_log', 'usage'),
        ('operator_registry', 'signature'),
        ('operator_registry', 'rollout'),
        ('operator_registry', 'metrics_snapshot'),
        ('audit_log', 'detail'),
)

# (索引名, 表, 列…)：租户过滤 + 时间排序的常用路径。
_COMPOSITE: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_telemetry_tenant_created", "telemetry_events", ("tenant_id", "created_at")),
    ("ix_cand_tenant_created", "candidates",
     ("tenant_id", "created_at", "candidate_id")),
    ("ix_reports_tenant_created", "reports",
     ("tenant_id", "created_at", "investigation_id")),
    ("ix_alerts_tenant_created", "alerts", ("tenant_id", "created_at", "event_id")),
    ("ix_job_tenant_submitted", "investigation_job",
     ("tenant_id", "submitted_at", "job_id")),
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _create_indexes() -> None:
    for name, table, cols in _COMPOSITE:
        op.create_index(name, table, list(cols), unique=False)


def _drop_indexes() -> None:
    for name, _table, _cols in _COMPOSITE:
        op.drop_index(name)


def upgrade() -> None:
    if _is_postgres():
        for table, col in _JSON_COLUMNS:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE jsonb "
                       f"USING {col}::jsonb")
    _create_indexes()


def downgrade() -> None:
    _drop_indexes()
    if _is_postgres():
        for table, col in _JSON_COLUMNS:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE json "
                       f"USING {col}::json")