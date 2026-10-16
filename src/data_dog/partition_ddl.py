"""分区**结构变更（DDL）**的单持有者互斥（架构评审 A9 的多副本收口）。

为什么需要这个模块
----------------
`telemetry_events` 的分区维护是**结构变更**：`--ensure-months` 建分区、`--drop-before`
删分区、`--convert` 就地转分区表。此前这几件事只有两层保护——**单事务**（失败整体回滚）
与**文档化的停机窗口**。那是在保护"一个人的操作"，挡不住"多个入口同时动手"：

- `DD_PARTITION_AUTO_ENSURE=1` 时**每个副本启动都会补分区**——滚动发布 = N 个进程同时 DDL；
- 集群 CronJob（helm `partition.enabled`）与运维手工 `--ensure-months` 可能重叠；
- `--convert` 撞上 `--drop-before` 时，第二个会撞表锁、抛出难读的错。

`retention:purge`（架构评审 A6）已经用 `state/lease.py` 解决过同一类问题。这里**复用同一把
原语**，只换一个 key：抢不到就**本轮跳过**（不是失败），并如实说清"被谁挡的"。

它保证什么、不保证什么（诚实边界）
--------------------------------
- **保证**：同一时刻只有一个"守规矩的"入口在做结构变更；"谁在做"可查（`lease_owner`）。
- **不保证**：它**不是**转换正确性的边界——PG 自己的表锁（`RENAME` 要 `ACCESS EXCLUSIVE`）
  才是。两个 DDL 撞上时第二个会被 PG 挡住，**不会坏数据**。租约省掉的是"白干 + 表锁堆积 +
  日志变吵 + 说不清谁在动结构"。
- 租约是**协作式**的（见 `state/lease.py`）：持有者崩溃后要等 TTL 过期别人才接管。所以 TTL
  必须**大于最慢的那次 DDL**（默认 900s，覆盖百万行级的整表拷贝），否则长 DDL 会被第二个副本
  抢走——那种情况下 PG 锁仍然兜底，但互斥就失效了。
- **memory 后端不提供跨副本保证**（各副本各一份状态）——多副本必须让 `DD_STATE_BACKEND` 落在
  postgres/redis；`data-dog config check` 对"自动补分区 + memory"直接报警。

两个入口共用同一把租约
--------------------
- 运维 CLI：`scripts/partition_telemetry.py`（`--no-lease` 或 `DD_PARTITION_DDL_LEASE=0` 可显式关掉）；
- 应用启动兜底：`service/partition_maintenance.ensure_if_enabled()`。

只读动作（`--check` / `--dry-run`）**不参与互斥**：它们不抢锁，别人持锁也照常给出现状与计划。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from . import settings
from .state import Lease, default_owner, get_state_store

# 与 `retention:purge` / `file:source` 同一把 Lease 原语，只是换了 key
# （key 会出现在共享状态里，命名要能直接回答"谁在动结构变更"）
PARTITION_DDL_LEASE_KEY = "partition:ddl"


def _new_lease() -> Lease:
    """按当前配置造一把租约实例（不抢）。owner = host:pid，TTL 见 settings。"""
    return Lease(get_state_store(), PARTITION_DDL_LEASE_KEY, default_owner(),
                 ttl_s=settings.partition_ddl_lease_ttl_s())


def acquire_ddl_lease() -> Lease | None:
    """抢"分区 DDL"租约；抢不到返回 None（调用方当作"本轮跳过"）。"""
    holder = _new_lease()
    return holder if holder.acquire() else None


def ddl_lease_holder() -> str:
    """当前持有者标识（无主/已过期返回空串）——用于报告"谁在做结构变更"。"""
    return _new_lease().holder()



def lease_age_seconds() -> float | None:
    """租约从上次获取/续期算起的秒数；无主/已过期 → None。

    记录里只有 owner/expires_at，而 expires_at = 获取时刻 + TTL，所以能从当前配置反推；
    **不改共享状态格式**，免得滚动发布中的新旧副本互相误读。若持有者用的是不同 TTL，
    算出的 age 会偏——它只喂告警、不参与互斥判定，可接受。
    """
    rec = _new_lease().state.get(PARTITION_DDL_LEASE_KEY)
    if not isinstance(rec, dict) or not rec.get("owner"):
        return None
    try:
        expires_at = float(rec.get("expires_at", 0) or 0)
    except (TypeError, ValueError):
        return None
    renewed_at = expires_at - settings.partition_ddl_lease_ttl_s()
    return max(0.0, datetime.now(timezone.utc).timestamp() - renewed_at)


def record_skip(reason: str, sink: Any) -> None:
    """把这轮被让路记一笔到指定 counter（例：metrics.partition_ddl_skipped_total）。

    counter 当参数传进来而不 import metrics：存储层不得 import core（分层断言），
    而且 CLI 没有 Prometheus registry。
    """
    sink.labels(reason=reason).inc()


@contextmanager
def ddl_guard(*, lease: bool | None = None) -> Iterator[str | None]:
    """分区**结构变更**的单持有者互斥上下文。

    产出值（`blocker`）只有两种语义，调用方照它分支即可：

    - `None` → **可以执行 DDL**（拿到租约，或租约被显式关闭）；
    - `str`  → **不要执行**：别的进程/副本正持有租约，值是那个持有者的标识
      （连标识都读不到时退化为 `"?"`）。

    为什么产出 `blocker` 而不是抛异常 / 返回 bool：两个调用方（CLI 与启动钩子）都要把
    "跳过了、被谁挡的"**如实报出去**——那是"这轮不该我干"，不是"我干完了"。
    """
    enabled = settings.partition_ddl_lease() if lease is None else lease
    if not enabled:
        yield None                      # 显式关闭：不碰共享状态（单机直跑 / 已确认无并发）
        return
    holder = acquire_ddl_lease()
    if holder is None:
        yield ddl_lease_holder() or "?"
        return
    try:
        yield None
    finally:
        holder.release()                # 干完就还锁：别人不必等到 TTL 过期


__all__ = [
    "PARTITION_DDL_LEASE_KEY",
    "acquire_ddl_lease",
    "ddl_guard",
    "ddl_lease_holder",
    "lease_age_seconds",
    "record_skip",
]
