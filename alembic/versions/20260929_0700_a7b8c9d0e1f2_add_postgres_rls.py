"""add PostgreSQL Row-Level Security (租户行级隔离的数据库强制层)

背景
----
多租户隔离此前是**应用层**的：`tenant_id` 进复合主键、仓储层每个读写都带
`WHERE tenant_id = :t`。这让跨租户读不可能**只要应用代码正确**。数据库层面
没有任何东西阻止一条写错的 SQL（漏了 WHERE）读到别的租户。

本迁移加第二层：PostgreSQL 的 Row-Level Security——策略由**数据库**执行，
应用漏写过滤也读不到别人的行。

工作方式
--------
- 每个租户表启用 RLS + 策略：`USING (tenant_id = current_setting('app.tenant_id'))`。
- 应用在事务内 `SET LOCAL app.tenant_id = '<t>'`（见 repository.set_tenant），
  策略据此放行本租户的行。`SET LOCAL` 是事务作用域，事务结束自动失效。
- 读写都受限：`USING` 管读，`WITH CHECK` 管写（防写入他人租户的行）。

诚实边界
--------
1. **只对 PostgreSQL 生效**。SQLite 无 RLS，本地开发仍是应用层过滤；生产连 PG
   才有这一层。迁移在 SQLite 下是 no-op（见下）。
2. **超级用户与表属主默认绕过 RLS**。生产连接应以**非属主、非 superuser** 的角色
   运行（如 `data_dog_app`），否则策略形同虚设。`FORCE ROW LEVEL SECURITY` 让属主
   也受限（本迁移已加），但 superuser 仍绕过——故连接角色必须不是 superuser。
3. `current_setting('app.tenant_id', true)` 的 `true`（missing_ok）：未设上下文时
   返回 NULL → `tenant_id = NULL` 为 NULL（非 true）→ **零行可见**（失败关闭）。
   这正是想要的：忘记设租户 = 什么都读不到，而非读到全部。

可逆：downgrade 删策略 + 关 RLS。
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = 'a7b8c9d0e1f2'
down_revision: str | None = 'f6a7b8c9d0e1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 带 tenant_id 列的表（state_kv 是全局状态，无租户维度，不加 RLS）
_TENANT_TABLES = (
    "telemetry_events",
    "candidates",
    "reports",
    "alerts",
    "alert_outbox",
    "alert_feedback",
    "knowledge_documents",
    "knowledge_chunks",
)

_PREDICATE = "tenant_id = current_setting('app.tenant_id', true)"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        # SQLite 等无 RLS：跳过（本地开发仍是应用层过滤，诚实边界见 docstring）
        return
    for t in _TENANT_TABLES:
        # FORCE：连表属主也受限（生产连接角色仍须非 superuser，见 docstring）
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {t} "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    if not _is_postgres():
        return
    for t in _TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
