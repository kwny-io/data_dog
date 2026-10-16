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

import time
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


# 结构变更的**结果**（成功/失败）放在共享 state 里，而不是进程内变量。
# 为什么必须共享：失败发生在 CronJob / 启动钩子那个进程里，而 Prometheus 抓的是 service 的
# /metrics；两个进程之间只有共享 state（`DD_STATE_BACKEND`）能传话。key 只存一个 dict
# （op -> 记录），一次 get 读全，避免多次往返与"读到一半"的中间态。
#
# 为什么跳过（租约 / 咨询锁被占）**不**记在这里：那是"这轮不该我干"（CLI 退出码 3），什么都
# 没做，不欠谁一个解释；它已经有 `partition_ddl_skipped_total{reason}` 这个 counter。
PARTITION_DDL_RESULT_KEY = "partition:ddl_result"

# 允许出现在指标标签里的动作白名单。**有限枚举**：共享 state 里的脏值不能变成 Prometheus 的
# 无限基数（与"租约 owner 不做标签"同一条纪律）。
DDL_OPS = ("ensure", "drop", "convert", "unpartition", "backfill")


def record_result(op: str, *, ok: bool, kind: str = "", detail: str = "") -> None:
    """记一次结构变更的结果（幂等；只改共享 state 里的一个 key）。

    `kind` 只在失败时有意义：`refused`（前置条件 / 能力边界不满足，CLI 退出码 2）、
    `error`（真异常）。成功会把连续失败数**清零**，失败则 +1。成功也可以带 `detail`
    （例：backfill 记 "updated=N batches=M"）——它是"这次做了什么"的唯一留痕。

    写共享 state 失败**不影响主流程**——那是一条记账路径，不是 DDL 本身，把"记账失败"升级成
    "迁移失败"只会更糟。真出错时这次结果就是没记上（指标里表现为没有变化），不撒谎。
    """
    now = time.time()

    def _apply(cur: Any) -> tuple[Any, None]:
        data = dict(cur) if isinstance(cur, dict) else {}
        prev = dict(data.get(op) or {})
        if ok:
            data[op] = {"ok": True, "fails": 0, "at": now, "ok_at": now,
                        # 失败历史不该被下一次成功抹掉：连续失败数会清零（它要回答"现在
                        # 是不是坏的"），但"上次什么时候坏过"只能靠时间戳，得留着。
                        "fail_at": prev.get("fail_at") or 0,
                        "kind": "",
                        "detail": str(detail)[:500]}   # 成功也留痕（例：backfill 更新了多少行）
        else:
            data[op] = {
                "ok": False,
                "fails": int(prev.get("fails") or 0) + 1,
                "at": now,
                "ok_at": prev.get("ok_at") or 0,     # 成功时间戳要跨失败保留
                "fail_at": now,
                "kind": kind,
                "detail": str(detail)[:500],          # 日志里可以有长文本，状态里截断
            }
        return data, None

    try:
        get_state_store().mutate(PARTITION_DDL_RESULT_KEY, _apply)
    except Exception:                        # noqa: BLE001 — 记账失败不该让迁移本身报错
        return


def ddl_results() -> dict[str, Any]:
    """读回所有动作的结果记录（读不到 / 类型不对 → 空 dict）。

    只返回白名单里的 op：共享 state 可能被手工改过或来自旧版本，脏 key 不该进指标标签。
    """
    try:
        raw = get_state_store().get(PARTITION_DDL_RESULT_KEY)
    except Exception:                        # noqa: BLE001
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in DDL_OPS and isinstance(v, dict)}


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
    "DDL_OPS",
    "PARTITION_DDL_LEASE_KEY",
    "PARTITION_DDL_RESULT_KEY",
    "ddl_results",
    "acquire_ddl_lease",
    "ddl_guard",
    "ddl_lease_holder",
    "lease_age_seconds",
    "record_result",
    "record_skip",
]
