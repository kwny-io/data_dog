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

---

## 8. 实现与验收（已落地）

### 8.1 落地的文件

| 文件 | 动作 | 说明 |
|---|---|---|
| `src/data_dog/stream/sharding.py` | **新建** | `shard_key` / `pick_shard` / `pick_owner`（rendezvous）+ `ShardRouter` |
| `src/data_dog/stream/runner.py` | 改 | `StreamRunner` 加 `router` 参数 + `_owns()` 过滤（默认不过滤） |
| `src/data_dog/stream/factory.py` | 改 | **新建** `build_router(source)`：按源协调模式决定是否启用 |
| `src/data_dog/stream/__init__.py` | 改 | 导出 `ShardRouter` / `build_router` / `pick_owner` / `pick_shard` / `shard_key` |
| `src/data_dog/settings.py` | 改 | 加 `DD_REPLICA_ID` / `DD_REPLICA_MEMBERS` / `DD_SHARD_COUNT` |
| `src/data_dog/core/metrics.py` | 改 | 加 `shard_skipped_total` / `shard_enabled` |
| `src/data_dog/service/app.py` | 改 | 起流时装配 router + 设 gauge |
| `src/data_dog/service/stream_worker.py` | 改 | 独立消费者进程装配 router |
| `helm/data-dog/templates/deployment-stream.yaml` | 改 | downward API 注入 `DD_REPLICA_ID`（Pod 名）+ 成员列表 |
| `helm/data-dog/values.yaml` | 改 | 加 `stream.replicaMembers` |
| `tests/test_sharding.py` | **新建** | 24 条 |

### 8.2 一处「看似对、其实会静默丢数据」的设计坑（已在代码层钉死）

**坑**：`ShardRouter.owns()` 对「自己不在成员列表」的副本返回**恒假**——本意是"失败
关闭，不抢别人的键"。但若成员列表配错（典型：用 Deployment 的随机 Pod 名去对一份静态
成员列表），**每个副本都认为自己不拥有任何键 → 所有消息被跳过 → 静默丢数据**，且无
报错（`shard_skipped` 疯涨但没人看）。

**修法**：`build_router` 加自检——**配了成员列表而本副本 id 不在其中 → 启动即抛错**，
拒绝启动（与 `stream/factory.py` 的 file+多副本自检同一哲学）。要正确用分片，须让副本
id 稳定可枚举（StatefulSet 的 `stream-0/1/2`），或对 kafka/mqtt（broker 已分区）**留空**
成员列表。测试 `test_build_router_self_not_in_members_fails_closed` 钉住此路径。

### 8.3 为什么分片过滤**不**对 kafka/mqtt 启用（关键正确性）

broker 端（Kafka 消费者组 / MQTT 共享订阅）**已经**把每条消息派给唯一副本。若客户端再
叠加一层 rendezvous 过滤，且两侧判定**不一致**，消息会：既不被 broker 派到属主、又被
当前副本以"不归我"丢弃 → **丢事件**。故 `build_router` **只在
`source.coordination()=="none"`**（每副本收全量）时启用。这是"两层分区叠加"的正确性陷阱，
测试 `test_build_router_disabled_for_broker_group` 钉住。

### 8.4 验收对照（§5 六条）

| # | 验收项 | 怎么验 | 状态 |
|---|---|---|---|
| 1 | 确定性路由 + 分布均匀 | `test_pick_owner_deterministic` / `test_distribution_reasonably_uniform`（1000 键 4 副本，各 150–350） | ✅ |
| 2 | 最小扰动（N→N+1 迁移 ≈1/N） | `test_minimal_disruption_on_add/remove_replica`（4→5 迁移 12%、4→3 迁移 25%） | ✅ |
| 3 | 同设备不跨副本漂移 | `test_runner_same_device_never_split_across_replicas` / `test_runner_filters_non_owned_keys` | ✅ |
| 4 | 限流总量正确 | **不适用**——限流桶本就按 `tenant\|device` 在**共享后端**（G5），分片只改"谁处理"、**不改令牌桶键**，故总量恒 = 配置值。**未新增专门测试**（语义未变，非"已测通过"） | ➖ |
| 5 | 再平衡无丢失 | **部分**——路由重映射已验（§5.2）；"双读"防丢**未实现**（见诚实边界） | 🟡 |
| 6 | >100 台吞吐实测 | 见 §8.5 | ✅ |

### 8.5 吞吐实测（`loadgen`，>100 台）

**环境**：本机 Windows 11 / Python 3.12.6 / SQLite + 离线 provider（排除 DB/LLM 外因）
**命令**：`python tools/loadgen.py --url http://127.0.0.1:8821 --mode write --devices 120 --rps 300 --duration 15 --tenant bench-p45 --verify-server --assert-no-llm`

| 场景 | 设备数 | 目标 RPS | 实测吞吐 | P50 | P95 | P99 | max | 错误 |
|---|---|---|---|---|---|---|---|---|
| 遥测写入（120 台） | 120 | 300 | **36 req/s** | 3040ms | 6078ms | **7843ms** | 9782ms | **0** |

- 两侧一致性（客户端 vs 服务端直方图）偏差 0.2% ✓；热路径零 LLM ✓。
- **36 req/s 与 P4-8 baseline 形态 A（35 req/s）一致**——说明分片逻辑本身**不引入额外开销**
  （过滤是纯内存哈希，未走 DB）。此处量的是**单实例热路径写入**，非"分片后的集群总吞吐"。
- **为何不更高**：本机 SQLite 单写入者串行化是主因（见 `benchmarks/baseline.md` 归因），
  **与分片无关**。分片的收益在**多实例集群**：N 个副本各处理 1/N 的键，总吞吐近线性放大，
  但**本机单进程无法实测集群总吞吐**——故此处如实只报单实例数字。

### 8.6 诚实边界（补充 §6）

- **再平衡"双读"未实现**（§5.5 部分达成）：路由能在成员变更瞬间正确重映射，但**已在途**
  的消息可能仍发给旧属主——迁移窗口内的重复/丢失**未被双读消除**。要严格无丢需事务性
  消费或迁移期双读，属更大工程。当前实现**不声称** exactly-once。
- **分片过滤只在"每副本收全量"的源生效**：kafka/mqtt 靠 broker 分区，**客户端不分片**。
  故"车队级分片"当前仅在**无协调的源**（如无共享订阅的 MQTT，或应用层扇出）上由本模块
  实现；对 kafka/mqtt 而言，分片是 **broker 的职责**，本模块提供的是**选择正确源与校验**。
- **本机吞吐 ≠ 集群吞吐**：§8.5 量的是单实例；集群总吞吐需在生产多副本环境实测。
- **成员列表一致性是外部依赖**：rendezvous 正确性依赖所有副本看到**相同**列表（K8s
  headless service / etcd / Redis）。列表不一致 → 归属判定分裂，本模块**无法自愈**。
