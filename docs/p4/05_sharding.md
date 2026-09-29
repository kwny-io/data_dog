# P4-5 车队级分片（>100 台）

## 1. 目标

从「几台机器狗」扩到「一个车队（>100 台）」。核心是把**单实例 StreamRunner** 换成
**分片消费**：按分片键把设备路由到固定副本，负载均衡且**同设备不跨副本漂移**（否则
窗口状态会分裂）。

## 2. 现状（已核实）

| 已有 | 位置 | 说明 |
|---|---|---|
| 分区键 | `data_plane/gateway.py:110` `partition_key(device_sn, component)` | **仅生成 key**，非分片路由 |
| 单实例消费 | `stream/runner.py` `StreamRunner` | 单源单循环 |
| 共享状态 | `state/` | 已可外置（水平扩展基础） |

**缺**：分片路由表、一致性哈希 / rendezvous 哈希、分片级限流、分片再平衡。相关在途任务 #25。

## 3. 设计

### 3.1 分片键

- 默认 **`tenant_id|device_sn`**（同设备所有事件必须落同分片——窗口状态不分裂）。
- 可选更细：`tenant|device|component`（若同设备部件可独立并行）。
- 分片数 `N`（如 256）**远大于**副本数，便于均衡与再平衡。

### 3.2 路由：rendezvous（HRW）哈希

选**最高**哈希权重的副本（而非取模）——副本增减时只迁移 `1/N` 的键，**最小扰动**：

```python
# stream/sharding.py（新建）
def pick_shard(key: str, shards: int) -> int:
    return crc32(key.encode()) % shards

def pick_owner(key: str, replicas: list[str]) -> str:
    # rendezvous: 每个副本算一个权重，取最大
    return max(replicas, key=lambda r: crc32(f"{key}#{r}".encode()))
```

### 3.3 消费模式

- **Kafka**：分片键 → partition（`partition = pick_shard(key, partitions)`）；消费者组天然分片。
- **MQTT**：按分片订阅不同 topic 前缀（`telemetry/shard/{i}`），或共享订阅 + 客户端侧再分。
- **HTTP 推送**：入口网关按 `pick_owner` 转发到对应副本（或写 Kafka 让下游分片）。
- **file 源**：本质单机，不适用分片（车队场景应过渡到 Kafka/MQTT）。

### 3.4 分片级限流

限流键 = `tenant|device`（现状已是）；分片后**每副本只对自有分片限流**，总量 = 各分片之和。
共享限流桶（`DD_RATE_BACKEND=redis`）保证全局口径一致。

### 3.5 再平衡

- 副本上下线 → rendezvous 自动重新映射**受影响**的键；
- 迁移期间**双读**（旧副本 flush 完再交棒），避免迁移窗口丢事件；
- 再平衡进度暴露为指标（`dd_shard_rebalance_inflight`）。

## 4. 要建/要改的文件

| 文件 | 动作 |
|---|---|
| `src/data_dog/stream/sharding.py` | **新建**：分片键 + rendezvous 路由 |
| `src/data_dog/stream/runner.py` | 改：`StreamRunner` 支持 `shard_of`/`owns(key)` 过滤 |
| `src/data_dog/stream/factory.py` | 改：装配分片参数（`DD_SHARDS`、副本成员） |
| `src/data_dog/data_plane/gateway.py` | 复用 `partition_key` | 
| `src/data_dog/settings.py` | 加 `DD_SHARDS`、`DD_REPLICA_ID`、`DD_REPLICA_MEMBERS` |
| `src/data_dog/core/metrics.py` | 加分片指标 |
| `tools/loadgen.py` | **新建**（与 P4-8 共用）：模拟 >100 台设备打流 |
| `tests/test_sharding.py` | **新建** |

## 5. 验收

1. **确定性路由**：同 key 多次 `pick_owner` 同结果；不同 key 分布均匀（卡方/直方图检查）。
2. **最小扰动**：N 副本→N+1，仅约 `1/(N+1)` 键迁移。
3. **不跨副本漂移**：同设备事件恒落同分片 → 窗口不分裂（断言归并正确）。
4. **限流总量正确**：分片后全局限流 = 配置值（不因分片放大）。
5. **再平衡**：模拟副本下线，键重映射后无事件丢失（双读验证）。
6. **吞吐**：用 `loadgen` 实测 >100 台吞吐，**报实测数字**（不空口保证）。

## 6. 诚实边界

- **吞吐数字以实测为准**：本文不写"支持 X 万 TPS"这类未验证声明——由 `loadgen` 跑出来填。
- **file 源不分片**：车队场景须过 Kafka/MQTT。
- **再平衡有短暂迁移窗口**：双读把丢事件风险降到最低，但**非零**（严格 exactly-once 需
  事务性消费，属更大工程）。
- rendezvous 是**客户端路由**：一致性依赖所有副本看到**相同**的成员列表（用 K8s
  headless service / etcd / Redis 维护成员视图）。

## 7. 依赖

- **缺口-5 多副本协调**（前置）：先有"不重复消费"，分片才有意义。
- **P4-4 K8s**（并列）：成员列表来自编排层。
- **P4-8 loadgen**（共用工具）：吞吐实测。
