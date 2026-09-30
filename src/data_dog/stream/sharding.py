"""P4-5 车队级分片：把「一个车队（>100 台设备）」按分片键路由到固定副本。

为什么需要分片
--------------
单实例消费到多副本消费，核心约束是：**同一台设备的所有事件必须落同一个副本**。
否则窗口状态（`SharedCandidateBuilder` 的候选分组）会在副本间分裂——半条轨迹在一
个副本、半条在另一个，两边各自 flush，产出两条残缺候选，谁也拼不出完整故障。

分片键
------
默认 **`tenant_id|device_sn`**（同设备不跨副本）。分片数 `N`（如 256）**远大于**
副本数，便于均衡与再平衡。可选把 `component` 纳入键（`tenant|device|component`），
让同设备的不同部件并行——本项目默认**不**纳入（同设备的部件事件常需合并成一条候选，
拆开会分裂状态）。

路由：rendezvous（最高随机权重，HRW）哈希
-----------------------------------------
副本增减时，取模哈希会让**几乎所有**键重新映射（缓存/状态全失效）；rendezvous 只
让**属于变动副本的**那一份键（约 `1/N`）迁移——**最小扰动**。做法：对每个键、每个
副本算一个权重，取**最大**权重者为属主::

    owner = max(replicas, key=lambda r: weight(key, r))

诚实边界
--------
- **本模块只做客户端路由**（纯函数）。它**不搬运数据**：真正的负载均衡取决于源——
  Kafka 靠消费者组按分区分配；MQTT 靠共享订阅或客户端过滤；HTTP 靠入口网关转发。
  一致性依赖**所有副本看到相同的成员列表**（K8s headless service / Redis / etcd 维护）。
- **不要与 broker 端按分区分配叠加使用**：Kafka 已按分区把消息给固定消费者，若再叠加
  本过滤且两者判定不一致，消息会被「我不拥有」的一侧丢弃而属主又收不到 → **丢事件**。
  故本过滤**只用于"每个副本都收全量"的源**（见 runner 文档）。
- **再平衡有短暂窗口**：成员列表变更瞬间，键立即重映射，但**已在途**的消息可能仍发给
  旧属主。严格 exactly-once 需事务性消费，属更大工程。
"""
from __future__ import annotations

import zlib

WILDCARD = "*"


def shard_key(*, tenant_id: str, device_sn: str, component: str = "") -> str:
    """分片键：默认 `tenant|device`；给 `component` 时用 `tenant|device|component`。

    同设备的窗口状态必须在同副本，故 device 必在键内；component 可选（默认不纳入）。
    """
    base = f"{tenant_id}|{device_sn}"
    return f"{base}|{component}" if component else base


def _weight(key: str, replica: str) -> int:
    """rendezvous 权重：`crc32(key#replica)`，取值确定性。"""
    return zlib.crc32(f"{key}#{replica}".encode()) & 0xFFFFFFFF


def pick_shard(key: str, shards: int) -> int:
    """把键映射到 `[0, shards)` 的分片号（取模，确定性）。"""
    n = max(1, int(shards))
    return (zlib.crc32(key.encode()) & 0xFFFFFFFF) % n


def pick_owner(key: str, replicas: list[str]) -> str:
    """rendezvous：返回键的属主副本。空列表 → `""`（无属主，调用方失败关闭）。

    同权重（哈希碰撞）时取**成员名最大**者——确定性，不随机。
    """
    if not replicas:
        return ""
    return max(replicas, key=lambda r: (_weight(key, r), r))


class ShardRouter:
    """一个副本的视角：持有成员列表，判定某分片键是否归**自己**。

    - 成员视图：`members`（集群里所有副本 id）。单副本 → `members=[self]` 或空，
      `owns` 恒真（不丢任何消息）。
    - `replica_id` 不成员列表 → `owns` 恒假？不：视为「游离副本」，仍按 rendezvous
      归属判定——若成员列表里没有自己，说明本副本不该消费（配置错），`owns` 恒假，
      让调用方**失败关闭**而非抢别人的键。
    """

    def __init__(self, *, replica_id: str, members: list[str],
                 shards: int = 256) -> None:
        self.replica_id = str(replica_id)
        # 去重 + 排序，保证所有副本看到同一份列表时算出同一结果
        self._members = sorted({str(m) for m in members if str(m)})
        self.shards = max(1, int(shards))

    @property
    def members(self) -> list[str]:
        return list(self._members)

    @property
    def enabled(self) -> bool:
        """单副本（≤1 成员）→ 不启用过滤（owns 恒真）。"""
        return len(self._members) > 1

    def shard_of(self, key: str) -> int:
        return pick_shard(key, self.shards)

    def owner_of(self, key: str) -> str:
        return pick_owner(key, self._members)

    def owns(self, key: str) -> bool:
        """该键是否归本副本。单副本 / 成员列表为空 → 恒真（不过滤）。"""
        if not self.enabled:
            return True
        return self.owner_of(key) == self.replica_id
