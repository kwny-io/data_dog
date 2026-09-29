# G4 降级队列持久化 + 接入 e2e

## 1. 目标

两件事：① 降级队列（`_queue`）现在**进程内 list，重启即丢**；改为可持久化后端。
② 降级段**未接进 `e2e/replay.py`** 整链回放；接进去，让降级路径也有端到端覆盖。

## 2. 现状（已核实）

| 已有 | 位置 | 说明 |
|---|---|---|
| 降级决策 | `agent/degradation.py:74-137` `DegradationPolicy` | `decide`/`decide_batch`/`drain`/`reset`；容量 `queue_capacity=100`，满则退化直报 |
| 队列 | 同上 `:84` | `_queue: list[str]` —— **纯进程内** |
| 调用方 | `agent/degradation_demo.py` | 仅演示；**未接入服务/存储** |
| 整链回放 | `e2e/replay.py:124-208` `run_e2e` | 回放 **P1→P2→Agent→P3→SaaS**，**不含** degradation 段（grep 确认） |

**缺**：队列持久化（注释已说"接真实 MQ 换 `_queue`"）、e2e 加降级段。

## 3. 设计

### 3.1 队列后端抽象（对齐 StateStore 的可插拔思路）

```python
# agent/degradation_queue.py（新建）
class QueueBackend(Protocol):
    def push(self, item: str) -> bool: ...        # 满 → False（调用方退化直报）
    def drain(self) -> list[str]: ...
    def __len__(self) -> int: ...
    def reset(self) -> None: ...

class InMemoryQueue: ...      # 现有语义，默认
class StateStoreQueue: ...    # 复用 state_kv：key="degradation:queue"（list），mutate 原子
class TableQueue: ...         # 可选：专用表（大队列/需要对账时）
```

- **默认**保持 `InMemoryQueue`（零行为变化）。
- **持久**：`DD_DEGRADATION_QUEUE=state` → `StateStoreQueue`，队列存共享状态 →
  **重启可续 + 多副本共享**（顺带解决多副本各持一份队列的问题）。
- `DegradationPolicy` 改为持 `QueueBackend`（构造注入），`decide`/`drain` 委托它——
  **决策逻辑不变**（这正是原设计为纯模块的用意）。

### 3.2 队列项结构（可持久需自描述）

现在 `_queue` 存 `str`（candidate_id）。持久化时改为 JSON 项：
`{"candidate_id","severity","enqueued_at","attempts"}`——便于恢复后重放与退避。

### 3.3 接入 e2e

在 `e2e/replay.py` 的 `run_e2e` 加**降级段**（在 P2→Agent 之间或旁路）：

```
... → 候选产出 → [降级决策] agent 不可用? → 硬规则直报 / 有界排队 / 正常调查 → ...
```

- **两种跑法**：`--with-degradation`（或配置）显式开启，避免改变既有回放的确定性输出
  （保持既有测试零回归）。
- 断言：agent 不可用时候选进队列/直报；可用时正常调查；队列满时退化直报。

### 3.4 服务侧（可选，本项不强求）

若接入服务：降级队列可作 `/v1/telemetry` 与调查之间的**缓冲**（agent 不可用时暂存候选）。
本项**先做队列持久化 + e2e**，服务接入按需。

## 4. 要建/要改的文件

| 文件 | 动作 |
|---|---|
| `src/data_dog/agent/degradation_queue.py` | **新建**：QueueBackend + 三实现 |
| `src/data_dog/agent/degradation.py` | 改：`DegradationPolicy` 持 QueueBackend（默认 InMemory） |
| `src/data_dog/e2e/replay.py` | 改：加降级段（开关控制） |
| `src/data_dog/settings.py` | 加 `DD_DEGRADATION_QUEUE`（memory/state） |
| `tests/test_degradation_queue.py` | **新建** |
| `tests/test_e2e.py`（或对应用例） | 加降级段断言 |

## 5. 验收

1. **持久往返**：`StateStoreQueue` push N 项 → 重建 policy（模拟重启）→ drain 得同 N 项。
2. **容量语义一致**：满 → `push` 返回 False（退化直报），与内存版一致。
3. **原子性**：并发 push/drain 不丢项（复用 StateStore.mutate）。
4. **多副本共享**：两个 policy 共享一个 `StateStoreQueue` → drain 不重复取。
5. **e2e 降级段**：开启开关 → 回放含降级决策；关闭 → 输出与现状**逐字节一致**（零回归）。
6. **零回归**：`test_degradation*`、`test_e2e*` 全绿。

## 6. 诚实边界

- **持久 ≠ 消息队列**：`StateStoreQueue` 是共享状态，**不是** Kafka/Redis Stream——无投递
  确认、无消费组。真 MQ 语义（at-least-once + 分区）需换后端；接口已为此留位。
- **队列项是 candidate_id 引用**：恢复后重放依赖**候选已落库**（候选真相在 `candidates` 表）。
- **e2e 降级段默认关**：保持既有回放的确定性输出不被打扰。
- **退避/重试不在决策层**：仍按原设计归传输层。

## 7. 依赖

- 与 **G3**（state 快照）同源：持久队列可被 checkpoint 覆盖。
- 无强前置，可独立做。
