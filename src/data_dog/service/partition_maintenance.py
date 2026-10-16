"""分区维护（A9）在生产里的两个动作：**报**与**补**。

为什么要这个模块
----------------
`storage/partitioning.py` 早就把分区能力做齐了，但**没人调用它**：CLI 要人工跑、helm 里
没有定时任务、应用启动也不碰。分区表最典型的线上事故是"忘了建下月分区 → 跨月那一刻写入
直接报 `no partition of relation ... found for row`"——而在这之前**没有任何信号**。

两个动作，职责刻意分开：

- `report_horizon(session)`：把"分区覆盖到什么时候"上报成
  `data_dog_partition_end_timestamp_seconds{kind="future"}`。**只读**，在 `/metrics` 抓取时
  调用（为什么上报时间戳而不是"还剩几天"，见 `core/metrics.py` 里那段注释）。
- `ensure_if_enabled()`：`DD_PARTITION_AUTO_ENSURE=1` 时在启动补齐未来 N 个月（幂等 DDL）。
  **默认关**——结构变更由集群层面的 CronJob（helm `partition.enabled`）做，应用副本不偷偷改结构；
  开了也只是多一层保险，失败不阻止启动，由指标 + 告警兜底。

  **多副本**：真的开的时候先抢 `partition:ddl` 单持有者租约（`partition_ddl.py`）——
  滚动发布 = N 个副本同时启动，只有抢到的那个补分区，其余**如实跳过**。
"""
from __future__ import annotations

import logging

from .. import partition_ddl, settings
from ..core import metrics
from ..partition_ddl import ddl_guard, lease_age_seconds
from ..storage import partitioning
from ..storage.db import session_scope

_log = logging.getLogger(__name__)


def report_horizon(session, table: str = partitioning.PARTITION_TABLE) -> float | None:
    """把分区有效期上报成 `data_dog_partition_end_timestamp_seconds{kind="future"}`。

    在 `/metrics` **每次抓取**时调用（值必须新鲜，理由见 core/metrics.py 里那段注释）。
    返回上界 epoch；None = 没上报（非 PG / 没有分区）——此时 Prometheus 里没有样本，
    chart 里那条 `absent(...)` 告警专门负责说"分区明明开着却没上报"。

    **为什么放在 service 而不是 storage**：`storage` 是叶子层（不得 import 任何内部子包，
    见 tests/test_layering.py）。读分区结构是 storage 的事，把它变成指标是服务层的事；
    上一条分层纪律正好逼出这个更干净的落点。
    """
    end = partitioning.horizon_epoch(session, table)
    if end is not None:
        metrics.partition_end_timestamp_seconds.labels(kind="future").set(end)
    return end


def report_lease() -> float | None:
    """上报分区 DDL 租约的持有状态：`lease_held`（0/1）与 `lease_age_seconds`。

    在 `/metrics` **每次抓取**时调用（与 `report_horizon` 同一处）。为什么需要它：租约让
    "谁在动结构"可查，但此前在 Prometheus 里完全不存在——"今天补分区被跳过"只能翻日志，
    "一个死进程占着租约、15 分钟后才被别人接管"更是完全看不见。返回持有年龄秒数（无主 → None）。
    """
    age = lease_age_seconds()
    metrics.partition_ddl_lease_held.set(1 if age is not None else 0)
    if age is not None:
        metrics.partition_ddl_lease_age_seconds.set(age)
    else:
        # 无主时写 0 而不是删样本：prometheus_client 的**无标签** Gauge 不支持 remove()
        # （实测 ValueError）。0 在这里语义也准确——"没有主"就是年龄 0；
        # 而 age 告警阈值是 2×TTL（默认 30 分钟），0 永远不会误触发。
        metrics.partition_ddl_lease_age_seconds.set(0)
    return age


def ensure_if_enabled() -> list[str]:
    """启动补齐未来分区，返回本次涉及的分区名；未开启 / 非分区表 → []。

    真错误（PG 连不上、DDL 被拒）**照常抛**，由调用方记日志——静默跳过和一个真错误
    不是一回事。唯一"安静返回"的情况是显式开了自动补齐、但表根本不是分区表：那是配置
    与部署形态不匹配，记 warning 后继续启动（服务本身可用）。
    """
    if not settings.partition_auto_ensure():
        return []
    months = settings.partition_ensure_months()
    # **多副本互斥**（架构评审 A9）：滚动发布时 N 个副本会同时走到这里。只有抢到
    # `partition:ddl` 租约的那个补分区，其余如实跳过——分区已经被那个持有者补齐了
    # （或马上会），都去抢表锁只会白干、还让日志变吵。
    with ddl_guard() as blocker:
        if blocker is not None:
            # 静默失败要配信号（本仓纪律）：跳过是事件，计入 counter 并上报租约状态。
            partition_ddl.record_skip("lease_held", metrics.partition_ddl_skipped_total)
            report_lease()
            _log.info("分区结构变更由 %s 持有，本次启动跳过自动补齐", blocker)
            return []
        with session_scope(system=True) as s:
            if not partitioning.is_partitioned(s):
                _log.warning(
                    "DD_PARTITION_AUTO_ENSURE=1，但 %s 不是分区表（非 PostgreSQL 或尚未转换），跳过",
                    partitioning.PARTITION_TABLE)
                return []
            created = partitioning.ensure_partitions(s, months=months)
            # 顺便报一次：刚补完的进程不该等到下一次抓取才有"有效期"这个信号。
            report_horizon(s)
        _log.info("分区有效期已补齐到未来 %d 个月：%s", months, ", ".join(created) or "(无变化)")
        return created
