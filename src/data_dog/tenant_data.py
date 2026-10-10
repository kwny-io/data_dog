"""租户级数据导出与删除（⑥）：合规里的"数据可携"与"被遗忘权"。

为什么这两件事必须成对出现
------------------------
只给导出不给删除（"被遗忘"）＝合规做一半；只给删除不给导出＝用户走的时候拿不到
自己的数据，同样不合规。两者都要**留痕**（`tenant.export` / `tenant.purge`），
而删除是**破坏性不可逆**动作，所以它比导出多两道闸：**只有运维面能调** +
**必须在请求里原样复述租户 id 作确认**（防"手滑删错租户"这类不可挽回的事故）。

表清单为什么是**动态发现**而不是写死
---------------------------------
按"哪些表带 `tenant_id` 列"从元数据里现算。写死清单的话，**将来新增一张租户表就会
默认不被导出、不被删除**——前者是数据不全，后者是**合规事故**（用户要求删除，你漏了
一张表）。动态发现把"漏表"变成不可能，代价是新增表会被自动纳入（这正是我们要的）。

边界（诚实说明，别当成完整的 GDPR 工具箱）
----------------------------------------
- 导出是**有界快照**（每表 `per_table_limit` 行），不是全量流式导出；超限会在
  `truncated` 里**显式标出**并对该表计数，**绝不静默截断**。
- 删除是**按表删行**，不是"加密擦除/密钥销毁"；**备份与只读副本上的残留不在本模块
  范围**（那要靠备份过期策略，属运维层面）。
- 删除**不动共享状态后端的运行期状态**（`state_kv` 里的限流桶/候选分组窗口：
  它们没有 `tenant_id` 列，会自然过期）——这是**明确的非目标**，写在这里免得被误认为已覆盖。
- 删除**保留租户 `-`**（无租户上下文的系统/运维事件）下的记录：它们不属于任何租户。
"""
from __future__ import annotations

from typing import Any

from . import audit

# 删除顺序：**子表在前、父表在后**（外键/悬挂行）。未列出的租户表会被自动补在后面，
# 所以新增表默认也会被删（见模块 docstring：漏一张表就是合规事故）。
_PURGE_ORDER: tuple[str, ...] = (
    "alert_delivery",
    "alert_outbox",
    "alert_feedback",
    "alerts",
    "reports",
    "candidates",
    "telemetry_events",
    "investigation_job",
    "knowledge_chunks",
    "knowledge_documents",
    "change_records",
    "operator_rollout_event",
    "operator_registry",
    "oncall_rota",
    "state_checkpoint",
    "audit_log",
)


def tenant_tables() -> list[str]:
    """所有**带 `tenant_id` 列**的表，按删除顺序（子表在前）排列。

    `audit_log` 放最后：它是本模块"自己也留痕"的落点——先把租户自己的审计清掉，
    再写"这次删除"的系统侧记录，那条记录才不会被自己删掉。
    """
    from .storage.models import Base

    have = {name for name, t in Base.metadata.tables.items() if "tenant_id" in t.c}
    ordered = [t for t in _PURGE_ORDER if t in have]
    ordered += sorted(have - set(ordered))
    return ordered


def export_tenant(tenant_id: str, *, per_table_limit: int = 1000,
                  audit_access: bool = True) -> dict[str, Any]:
    """导出某租户的数据快照（每表最多 `per_table_limit` 行）。

    `truncated` 为真表示**至少有一张表被上限截断**——调用方必须据此判断"这份快照
    是否完整"，不能把有界快照当成全量导出（要全量就分页/提高上限）。
    """
    from sqlalchemy import func, select

    from .storage.db import session_scope
    from .storage.models import Base

    if per_table_limit < 1:
        raise ValueError("per_table_limit 必须 ≥ 1")

    tables = tenant_tables()
    out: dict[str, Any] = {}
    counts: dict[str, int] = {}
    truncated_tables: list[str] = []
    total = 0

    with session_scope(tenant_id=tenant_id) as s:
        for name in tables:
            tbl = Base.metadata.tables[name]
            cond = tbl.c["tenant_id"] == tenant_id
            rows = s.execute(select(tbl).where(cond).limit(per_table_limit)).all()
            total_rows = int(s.execute(
                select(func.count()).select_from(tbl).where(cond)).scalar() or 0)
            counts[name] = total_rows
            if total_rows > len(rows):
                truncated_tables.append(name)
            # 只取真实列，且 JSON 列保持原样（不在这里做业务语义转换）
            out[name] = [{c.name: r._mapping[c.name] for c in tbl.columns}
                         for r in rows]
            total += len(rows)

    if audit_access:
        # 导出是**大批量读取**，本身是敏感动作（谁在什么时候把自己/别人的数据拷走了）
        audit.record(action=audit.ACTION_TENANT_EXPORT,
                     outcome=audit.OUTCOME_ALLOWED, tenant_id=tenant_id,
                     target="/v1/tenant/export",
                     detail={"rows": total, "tables": len(tables),
                             "truncated": truncated_tables,
                             "per_table_limit": per_table_limit})

    return {
        "ok": True,
        "tenant_id": tenant_id,
        "per_table_limit": per_table_limit,
        "row_counts": counts,
        "exported_rows": total,
        "truncated": bool(truncated_tables),
        "truncated_tables": truncated_tables,
        "data": out,
    }


def purge_tenant(tenant_id: str, *, batch_size: int = 500, max_batches: int = 200,
                 dry_run: bool = False) -> dict[str, Any]:
    """删除某租户的**全部**数据（被遗忘权）。破坏性、不可逆。

    `dry_run=True` 只统计不删（先看清楚要删多少，再决定动不动手）。

    实现口径：① 跨租户动作 → `session_scope(system=True)`（RLS 下租户会话删不到
    别人的行，而这里正是要删"某租户"的全部行；系统通道是**唯一**能一次覆盖所有
    租户表的会话形态）；② 仍是**有界分批**（与 ④ 共用 `delete_in_batches`），
    批数用满即标记 `capped`；③ 删完写一条**系统侧**审计（`tenant_id=None` →
    保留租户 `-`）——把"某租户已被删除"这件事记在系统名下，否则它会随该租户的审计
    一起被删掉，等于**没有证据证明删除发生过**。
    """
    from sqlalchemy import func, select

    from .storage.db import session_scope
    from .storage.models import Base
    from .storage.repository import delete_in_batches

    if not tenant_id or not tenant_id.strip():
        raise ValueError("tenant_id 不能为空")
    if tenant_id == audit.SYSTEM_TENANT:
        raise ValueError(
            f"拒绝删除保留租户 {audit.SYSTEM_TENANT!r}：它存放无租户上下文的系统/运维事件")
    if batch_size < 1 or max_batches < 1:
        raise ValueError("batch_size 与 max_batches 必须 ≥ 1")

    rows: list[dict[str, Any]] = []
    total = 0

    with session_scope(system=True) as s:
        for name in tenant_tables():
            tbl = Base.metadata.tables[name]
            cond = tbl.c["tenant_id"] == tenant_id
            if dry_run:
                n = int(s.execute(
                    select(func.count()).select_from(tbl).where(cond)).scalar() or 0)
                rows.append({"table": name, "deleted": 0, "would_delete": n,
                             "batches": 0, "capped": False})
                total += n
                continue
            deleted, batches = delete_in_batches(
                s, tbl, cond, batch_size=batch_size, max_batches=max_batches)
            rows.append({"table": name, "deleted": deleted, "batches": batches,
                         "capped": batches >= max_batches})
            total += deleted

    report = {"ok": True, "tenant_id": tenant_id, "dry_run": dry_run,
              "batch_size": batch_size, "max_batches": max_batches,
              "total": total, "tables": rows}

    if not dry_run and total > 0:
        # 记在**系统**名下（不是该租户名下）：否则这条"删除发生过"的证据会随
        # 该租户的 audit_log 一起被删掉。
        audit.record(action=audit.ACTION_TENANT_PURGE,
                     outcome=audit.OUTCOME_ALLOWED, tenant_id=None,
                     target="/v1/tenant/purge",
                     reason=f"租户 {tenant_id} 数据已删除，共 {total} 行",
                     detail={"purged_tenant": tenant_id, "total_deleted": total,
                             "tables": [{"table": r["table"], "deleted": r["deleted"],
                                         "capped": r["capped"]}
                                        for r in rows if r.get("deleted")]})
    return report
