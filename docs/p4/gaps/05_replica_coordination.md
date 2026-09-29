# G5 流式源多副本协调

## 1. 目标

消除"多副本重复消费"：file / MQTT 源在无协调时会**全量重复处理**（同一事件被多副本各算
一遍 → 候选重复、限流口径翻倍）。本项给各源正确的多副本语义。

## 2. 现状（已核实 —— Kafka 已 OK）

| 源 | 现状 | 多副本行为 |
|---|---|---|
| **Kafka** | `bus.py:96-157`：**用了消费者组**（`group_id` 默认 "data-dog"，`enable_auto_commit=True`，`auto_offset_reset="latest"`） | ✅ 分区协调，**已正确** |
| **MQTT** | `bus.py:42-93`：订阅 topic 原样透传，**未用共享订阅** | ❌ 多副本全量重复 |
| **file** | `file.py:32-103`：按偏移尾随，**零去重** | ❌ 多副本各读各偏移 → 重复 |
| runner/factory | 无协调逻辑 | — |

`stream/base.py` 头部已声明"只做进程内轮询，无消费者组协调"。

**缺**：MQTT 共享订阅封装；file 侧单消费者选举（租约）；协调抽象。

## 3. 设计

### 3.1 MQTT：共享订阅（最省事）

- 把订阅 topic 前缀化为 `$share/<group>/<topic>`（MQTT 5 / EMQX 等 broker 支持）。
- 同 group 的多个订阅者**由 broker 轮询投递**——天然负载均衡，**一个消息只给一个副本**。
- `DD_MQTT_SHARE_GROUP`（默认 `data-dog`）；若 broker 不支持共享订阅 → **启动时告警**并
  退回单副本语义（文档写明）。
- 实现：`MqttSource` 增加 `share_group` 参数，拼 topic 时加前缀。

### 3.2 file：租约选主（单消费者）

单文件无 broker 协调，故需**应用层选主**：同一时刻**只有一个**副本读该文件。

```python
# stream/lease.py（新建）
class Lease:
    """在共享状态上实现的租约：持有者定期续约，过期可被抢占。"""
    def __init__(self, state, key: str, owner: str, ttl_s: float): ...
    def acquire(self) -> bool:   # mutate(key): 无主或已过期 → 归我；否则失败
    def renew(self) -> bool: ...
    def release(self) -> None: ...
```

- 租约存 `state_kv`（`mutate` 原子）：`{owner, expires_at}`。
- 持有者后台**心跳续约**（`ttl/3`）；崩溃 → 租约过期 → 其它副本抢占。
- **只有持有者**跑 file 尾随；非持有者**空转等待**（不读文件）。
- `DD_STREAM_LEASE_TTL_S`（默认 30）。
- **偏移也要共享**（否则接管者从头/从尾读）：持有者把当前 `_off` 周期性写共享状态；
  接管者从**上次共享偏移**续读（`DD_STREAM_SHARE_OFFSET=1`）。

### 3.3 HTTP 推送源

推送源（`HttpPushSource`）**由外部决定投给谁**——若前面有 LB 轮询，天然分散，无重复。
若多副本都监听同端口则端口冲突（部署问题，非本项）。**无需协调**，但文档写明部署方式。

### 3.4 统一抽象（可选）

```python
# stream/base.py
class StreamSource:
    def coordination(self) -> str:
        """返回 'broker-group' | 'lease' | 'none'，供运维/就绪检查展示。"""
```

`/readyz` 暴露各源的协调模式与（file 源）自己是否持有租约——**一眼看出谁在读**。

## 4. 要建/要改的文件

| 文件 | 动作 |
|---|---|
| `src/data_dog/stream/lease.py` | **新建**：租约（基于 StateStore） |
| `src/data_dog/stream/bus.py` | 改：MQTT 共享订阅（`share_group`） |
| `src/data_dog/stream/file.py` | 改：租约门控 + 共享偏移 |
| `src/data_dog/stream/base.py` | 加 `coordination()` 钩子 |
| `src/data_dog/stream/factory.py` | 装配协调参数 |
| `src/data_dog/settings.py` | 加 share group / lease ttl / share offset |
| `src/data_dog/service/app.py` | `/readyz` 暴露协调状态 |
| `tests/test_lease.py`、`tests/test_stream_coordination.py` | **新建** |

## 5. 验收

1. **MQTT 共享订阅**：拼出的 topic 带 `$share/<group>/`；不支持时报清晰告警。
2. **file 选主**：两个副本竞争 → **只有一个**获得租约；另一个不读文件。
3. **接管**：持有者"崩溃"（不续约）→ 租约过期 → 另一副本接管，**从共享偏移续读**（不丢不重）。
4. **偏移共享**：接管者起始偏移 = 上次共享值。
5. **Kafka 不回归**：消费者组行为不变。
6. **就绪可见**：`/readyz` 显示协调模式与租约归属。

## 6. 诚实边界

- **file 源本质单消费者**：租约只是"选一个"——file 源**不能真正水平扩展**（要扩展须转
  Kafka/MQTT）。文档如实写明。
- **MQTT 共享订阅依赖 broker 支持**：老 broker 可能不支持 → 退回重复消费（告警）。
- **租约有接管窗口**：持有者崩溃到接管之间有 `ttl` 延迟（期间该源无人读，事件积压不丢——
  file 是持久化的，续读即可）。
- **偏移共享非原子**：偏移与数据的落库不是事务性的，接管可能**重放少量最后事件**——
  靠候选**幂等 upsert** 兜底（不产生重复候选）。

## 7. 依赖

- **G3**（共享状态/快照）提供租约存储基础。
- **P4-5 分片** 的前置：先"不重复"，再"按分片分工"。
