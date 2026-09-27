"""add tenant_id and composite primary keys

把行级多租户隔离落到 schema 上：五张表加 `tenant_id`，并把租户**纳入复合主键**。

为什么必须进主键，而不是只加列 + WHERE 过滤
--------------------------------------------
本项目的业务 ID 是**确定性派生**的、**不含租户**：
  candidate_id     = cand_<sha256(device_sn|component|signal#seq)>
  investigation_id = inv_<candidate_id>
  alert event_id   = alert_<sha256(device_ref:event_type:episode)>
  telemetry event_id 由原始日志信封给出
两个租户若出现同名 `device_sn`（现实中完全可能——各厂各用各的编号），
就会派生**相同的 id**。若 id 仍是全局唯一主键，后写入的租户会**静默覆盖**
前一个租户的行——这是数据损坏，且 WHERE tenant_id 过滤**修不掉**。
把 tenant_id 并入主键后，两租户各占一行，互不干扰。

迁移策略：显式重建表（跨方言一致）
----------------------------------
baseline 建的主键**没有名字**，而 SQLite 也不保存主键约束名，因此
`drop_constraint` 无法引用它。这里对改主键的四张表采用**显式重建**
（建新表 → 拷数据 → 删旧表 → 改名 → 建索引）——这正是 SQLite batch 模式
内部做的事，但写出来后在 SQLite 与 PostgreSQL 上是**同一条代码路径**，
行为可预测、可测试。新表的主键带名字（`pk_<table>`）。

- `telemetry_events`：`tenant_id` 列 baseline 已存在，重建时**保留原值**
  （不覆盖为 demo）。
- 其余四张改主键的表：既有行回填租户 = `demo`（与 settings.default_tenant()
  默认值一致），新表不带 server_default，后续漏传租户的写入在 DB 层直接报错
  （失败关闭），而非静默落到某租户。
- `alert_feedback`：保留代理主键，只加列 + 索引。

可逆：downgrade 重建回原主键（单列），并移除新增的 tenant_id 列
（`telemetry_events.tenant_id` 为 baseline 既有列，保留）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'a06f16a9eff1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 既有行回填的租户值（固定字面量：迁移必须可重放，不读运行期环境）。
_BACKFILL_TENANT = "demo"


def _s(n: int) -> sa.String:
    return sa.String(length=n)


# ---------------------------------------------------------------- 列定义

# (列名, 列对象) —— 新表（含 tenant_id 且为主键）的完整列，顺序即建表顺序。
_CANDIDATES_COLS = [
    ("tenant_id", _s(80)), ("candidate_id", _s(80)), ("device_sn", _s(80)),
    ("component_ref", _s(120)), ("signal", _s(80)), ("severity_hint", _s(20)),
    ("window_start", _s(40)), ("window_end", _s(40)), ("sample_count", sa.Integer()),
    ("rule_matches", sa.JSON()), ("anomaly_score", sa.Float()),
    ("evidence", sa.JSON()), ("evidence_refs", sa.JSON()),
    ("detector_provenance", _s(160)), ("created_at", sa.Float()),
]
_CANDIDATES_PK = ["tenant_id", "candidate_id"]

_REPORTS_COLS = [
    ("tenant_id", _s(80)), ("investigation_id", _s(120)), ("disposition", _s(30)),
    ("severity", _s(20)), ("confidence", sa.Float()), ("event_type", _s(120)),
    ("summary", sa.Text()), ("candidate_refs", sa.JSON()), ("hypotheses", sa.JSON()),
    ("missing_evidence", sa.JSON()), ("recommended_actions", sa.JSON()),
    ("correlation_group_id", _s(80)), ("model_provenance", sa.JSON()),
    ("created_at", sa.Float()),
]
_REPORTS_PK = ["tenant_id", "investigation_id"]

_ALERTS_COLS = [
    ("tenant_id", _s(80)), ("event_id", _s(80)), ("idempotency_key", _s(200)),
    ("device_ref", _s(120)), ("component_ref", _s(120)), ("event_type", _s(120)),
    ("state", _s(30)), ("severity", _s(20)), ("confidence", sa.Float()),
    ("detected_at", _s(40)), ("event_revision", sa.Integer()), ("summary", sa.Text()),
    ("recommended_actions", sa.JSON()), ("investigation_id", _s(120)),
    ("policy_version", _s(40)), ("history", sa.JSON()), ("created_at", sa.Float()),
    ("updated_at", sa.Float()),
]
_ALERTS_PK = ["tenant_id", "event_id"]

_OUTBOX_COLS = [
    ("tenant_id", _s(80)), ("idempotency_key", _s(200)), ("revision", sa.Integer()),
    ("payload", sa.JSON()), ("status", _s(20)), ("attempts", sa.Integer()),
    ("receipt", _s(120)), ("last_error", _s(200)), ("created_at", sa.Float()),
]
_OUTBOX_PK = ["tenant_id", "idempotency_key", "revision"]

_TELEMETRY_COLS = [
    ("tenant_id", _s(80)), ("event_id", _s(80)), ("device_sn", _s(80)),
    ("occurred_at", _s(40)), ("component_ref", _s(120)), ("metrics", sa.JSON()),
    ("status_codes", sa.JSON()), ("operating_condition", _s(40)),
    ("operator_id", _s(120)), ("operator_version", _s(40)),
    ("binding_revision", _s(120)), ("raw_log_ref", _s(200)),
    ("created_at", sa.Float()),
]
_TELEMETRY_PK = ["tenant_id", "event_id"]

# 索引：{表名: [(索引名, [列...]), ...]}
_INDEXES = {
    "candidates": [
        ("ix_candidates_tenant_id", ["tenant_id"]),
        ("ix_candidates_device_sn", ["device_sn"]),
        ("ix_candidates_component_ref", ["component_ref"]),
        ("ix_candidates_signal", ["signal"]),
        ("ix_cand_dev_comp", ["device_sn", "component_ref"]),
    ],
    "reports": [
        ("ix_reports_tenant_id", ["tenant_id"]),
        ("ix_reports_disposition", ["disposition"]),
    ],
    "alerts": [
        ("ix_alerts_tenant_id", ["tenant_id"]),
        ("ix_alerts_idempotency_key", ["idempotency_key"]),
        ("ix_alerts_device_ref", ["device_ref"]),
        ("ix_alerts_event_type", ["event_type"]),
        ("ix_alerts_state", ["state"]),
    ],
    "alert_outbox": [
        ("ix_alert_outbox_tenant_id", ["tenant_id"]),
        ("ix_alert_outbox_status", ["status"]),
    ],
    "telemetry_events": [
        ("ix_telemetry_events_device_sn", ["device_sn"]),
        ("ix_telemetry_events_component_ref", ["component_ref"]),
        ("ix_telemetry_events_tenant_id", ["tenant_id"]),
    ],
}


def _columns(name: str) -> list:
    return {
        "candidates": _CANDIDATES_COLS,
        "reports": _REPORTS_COLS,
        "alerts": _ALERTS_COLS,
        "alert_outbox": _OUTBOX_COLS,
        "telemetry_events": _TELEMETRY_COLS,
    }[name]


def _nullable(col_defs: list, name: str) -> set:
    """返回该表里允许 NULL 的列名集合（其余 NOT NULL）。"""
    nullable = {
        "reports": {"severity", "event_type", "correlation_group_id"},
    }.get(name, set())
    return nullable


def _create_table(table: str, target: str, pk_cols: list) -> None:
    """按 table 的列定义在 target 名下建表，主键为 pk_cols。"""
    cols = [sa.Column(cn, ct, nullable=(cn in _nullable(_columns(table), table)))
            for cn, ct in _columns(table)]
    op.create_table(target, *cols,
                    sa.PrimaryKeyConstraint(*pk_cols, name=f"pk_{table}"))


def _rebuild(table: str, pk_cols: list, backfill_tenant: str | None) -> None:
    """重建表以更换主键。backfill_tenant=None 表示保留原 tenant_id 值。"""
    tmp = f"{table}__new"
    col_names = [cn for cn, _ in _columns(table)]
    _create_table(table, tmp, pk_cols)

    # 拷数据：tenant_id 为常量回填，或原样保留。
    src_exprs = ", ".join(
        f"'{backfill_tenant}'" if (cn == "tenant_id" and backfill_tenant is not None)
        else cn
        for cn in col_names
    )
    op.execute(
        f"INSERT INTO {tmp} ({', '.join(col_names)}) "
        f"SELECT {src_exprs} FROM {table}"
    )
    op.drop_table(table)
    op.rename_table(tmp, table)
    for ix_name, ix_cols in _INDEXES[table]:
        op.create_index(ix_name, table, ix_cols)


def _drop_indexes(table: str) -> None:
    for ix_name, _ in _INDEXES[table]:
        op.drop_index(ix_name, table_name=table)


def upgrade() -> None:
    # 四张改主键且需回填 tenant_id 的表
    _rebuild("candidates", _CANDIDATES_PK, _BACKFILL_TENANT)
    _rebuild("reports", _REPORTS_PK, _BACKFILL_TENANT)
    _rebuild("alerts", _ALERTS_PK, _BACKFILL_TENANT)
    _rebuild("alert_outbox", _OUTBOX_PK, _BACKFILL_TENANT)
    # telemetry_events：tenant_id 已存在，保留原值，仅改主键
    _rebuild("telemetry_events", _TELEMETRY_PK, backfill_tenant=None)

    # alert_feedback：代理主键不变，只加列 + 索引
    op.add_column("alert_feedback",
                  sa.Column("tenant_id", _s(80), nullable=False,
                            server_default=_BACKFILL_TENANT))
    with op.batch_alter_table("alert_feedback", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_alert_feedback_tenant_id"),
                              ["tenant_id"], unique=False)
        batch_op.alter_column("tenant_id", existing_type=_s(80),
                              server_default=None)


def downgrade() -> None:
    # alert_feedback
    with op.batch_alter_table("alert_feedback", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_alert_feedback_tenant_id"))
        batch_op.drop_column("tenant_id")

    # 把复合主键还原为单列主键。
    # 做法：复用升级时的重建，但主键去掉 tenant_id，并丢弃该列。
    _downgrade_pk("candidates", "candidate_id")
    _downgrade_pk("reports", "investigation_id")
    _downgrade_pk("alerts", "event_id")
    _downgrade_pk("alert_outbox", ["idempotency_key", "revision"])
    # telemetry_events：保留 tenant_id 列（baseline 即有），仅单列主键
    _downgrade_pk("telemetry_events", "event_id", keep_tenant=True)


def _downgrade_pk(table: str, old_pk, keep_tenant: bool = False) -> None:
    """还原为单列/原主键；keep_tenant=False 时同时删掉 tenant_id 列。"""
    old_pk_cols = [old_pk] if isinstance(old_pk, str) else list(old_pk)
    tmp = f"{table}__old"
    all_cols = [cn for cn, _ in _columns(table)]
    keep_cols = ([cn for cn in all_cols if cn != "tenant_id"] if not keep_tenant
                 else all_cols)

    # 预检：降级会把租户维度合并掉。若两个租户存在相同旧主键（例如同名
    # candidate_id），它们无法同时落回单列主键——此时**明确报错并中止**，
    # 而不是让 DB 抛裸 IntegrityError 或静默丢行。这是有意的失败关闭：
    # 操作者需先人工去重，再降级。
    dup = op.get_bind().exec_driver_sql(
        f"SELECT {', '.join(old_pk_cols)} FROM {table} "
        f"GROUP BY {', '.join(old_pk_cols)} HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if dup is not None:
        raise RuntimeError(
            f"无法降级表 {table}：旧主键 {tuple(old_pk_cols)} 在多租户下有重复值 "
            f"（例如 {tuple(dup)}）。降级会合并租户、导致无法区分或丢行。"
            f"请先人工去重（保留所需的租户行）后再执行 downgrade。"
        )

    cols = [sa.Column(cn, ct, nullable=(cn in _nullable(_columns(table), table)))
            for cn, ct in _columns(table) if cn in keep_cols]
    op.create_table(tmp, *cols,
                    sa.PrimaryKeyConstraint(*old_pk_cols, name=f"pk_{table}"))
    op.execute(
        f"INSERT INTO {tmp} ({', '.join(keep_cols)}) "
        f"SELECT {', '.join(keep_cols)} FROM {table}"
    )
    op.drop_table(table)
    op.rename_table(tmp, table)
    for ix_name, ix_cols in _INDEXES[table]:
        if all(c in keep_cols for c in ix_cols):
            op.create_index(ix_name, table, ix_cols)
