"""RLS 策略加一条**显式的系统扫描**通道（修一个静默失效）

背景（这个缺陷是实测出来的，不是推测）
---------------------------------------
RLS 策略是 `tenant_id = current_setting('app.tenant_id', true)`；未设上下文时右侧为
NULL → **零行可见**（失败关闭，这是对的）。但**后台扫描本来就没有租户**：

- `EscalationEngine.sweep_once` 要跨租户找出"到期未 ack"的投递；
- 异步 job 的卡死恢复扫描同理。

实测（非 owner / 非 superuser 的应用角色 + 本仓迁移 + PG 16）：

    (superuser)              alert_delivery 行数 = 1
    (app role) 未设 tenant_id 行数 = 0     ← 跨租户扫描看到的
    (app role) 设了 tenant_id 行数 = 1

后果：`sweep_once()` 永远 `scanned=0` → **告警升级在生产 PG 上根本不会发生**，
而且不报错。CI 抓不到是因为 CI 的 PG 腿连的是 superuser（superuser 绕过 RLS）。

本迁移做什么
------------
把每张**已有 `tenant_isolation` 策略**的表按新谓词重建策略：

    USING (
      tenant_id = current_setting('app.tenant_id', true)
      OR coalesce(current_setting('app.system_scan', true), 'off') = 'on'
    ) WITH CHECK (同)

应用侧只在**后台扫描**那两处传 `session_scope(system=True)`（全仓可 grep），
逐条处理仍走带租户的会话。**未设上下文依旧零行**——失败关闭不放松。

为什么动态查 `pg_policies` 而不是写死表名
----------------------------------------
租户表是**分批**加进来的（P0 八张、P4-2 算子注册表、P4-3 值班/投递、G1 job、
G7 变更记录……）。写死清单就会漏掉以后新加的表——而"漏掉"的表现正是本迁移要修的
那种静默失效。按策略名查则自动覆盖全部，包括将来新增的。

可逆：downgrade 把策略恢复成只有租户分支。
"""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = 'b7c8d9e0f1a2'
down_revision: str | None = 'a3b4c5d6e7f8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY = "tenant_isolation"
_TENANT_ONLY = "tenant_id = current_setting('app.tenant_id', true)"
_SYSTEM_OR = (
    "tenant_id = current_setting('app.tenant_id', true) "
    "OR coalesce(current_setting('app.system_scan', true), 'off') = 'on'"
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _tables_with_policy() -> list[str]:
    rows = op.get_bind().execute(text(
        "SELECT tablename FROM pg_policies "
        "WHERE policyname = :p AND schemaname = current_schema() "
        "ORDER BY tablename"), {"p": _POLICY}).fetchall()
    return [r[0] for r in rows]


def _rewrite(predicate: str) -> None:
    for t in _tables_with_policy():
        op.execute(f"DROP POLICY {_POLICY} ON {t}")
        op.execute(
            f"CREATE POLICY {_POLICY} ON {t} "
            f"USING ({predicate}) WITH CHECK ({predicate})")


def upgrade() -> None:
    if not _is_postgres():
        return                      # SQLite 无 RLS，no-op（与既有 RLS 迁移一致）
    _rewrite(_SYSTEM_OR)


def downgrade() -> None:
    if not _is_postgres():
        return
    _rewrite(_TENANT_ONLY)
