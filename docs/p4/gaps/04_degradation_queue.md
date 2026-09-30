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

## 8. 实现状态（已落地）

已实现，全量套件绿（新增 `tests/test_degradation_queue.py` 13 项；`test_degradation.py`
13 项、`test_e2e.py` 5 项零回归）。落地时相对本设计的调整，如实记录：

- **`push` 把「查长度 + 追加」合并为一步原子操作**，容量由参数传入。原因：旧代码在
  决策函数里 `len(queue) >= capacity` 判断，多副本下各算各的长度会**超发**；`StateStoreQueue`
  借 `mutate` 的原子性天然避免。`push` 返回 `bool`（满→False），调用方退化为直报。
- **`DegradationPolicy` 新增 `queue: QueueBackend` 构造注入**（默认 `InMemoryQueue`，
  与旧行为逐字节一致）；新增 `drain_items()` 返回完整 `QueueItem`（含 severity/attempts，
  供恢复后重放），原 `drain()` 仍返回 `candidate_id` 列表（保持既有调用方契约）。
- **队列项 `QueueItem` 自描述**：`{candidate_id, severity, enqueued_at, attempts}` 的 JSON；
  `from_dict` **宽容解析**——缺字段用默认值，跨版本恢复不因少字段而崩；底层状态被写成
  非 list（脏数据）时当作空队列，不崩。
- **后端选择走 `settings.degradation_queue_backend()`**（`DD_DEGRADATION_QUEUE`，默认
  `memory`；`state` → `StateStoreQueue`）。`state` 要真跨进程持久/共享，`DD_STATE_BACKEND`
  须落在 postgres/redis——memory 状态后端只在同进程内共享（模拟"重启"够用、真重启不够）。
- **e2e 降级段默认关**（`run_e2e(with_degradation=True)` 或 CLI `--with-degradation`）：
  关闭时 `summary()` **不含** `degradation` 键，与既有回放逐字节一致（零回归，已实测）。
  该段是**旁路观测**——不改主链的 reports/decisions。
- **e2e 实际只跑出 1 个候选且命中硬规则 → `direct_report`**，队列未被撑起（`queued: 0`）。
  如实说明：现有 e2e 输入不足以触发排队路径；排队/溢出/恢复的行为由单测覆盖。

仍未解（诚实边界，同 §6）：**持久 ≠ 消息队列**（`StateStoreQueue` 是共享状态，不是
Kafka——无投递确认/消费组/分区；真 MQ 语义需换后端，接口已留位）；**队列项是 candidate_id
引用**（恢复后重放依赖候选已落库，真相在 `candidates` 表）；**退避/重试不在决策层**
（`attempts` 只做记录，退避属传输层）；**服务侧接入未做**（§3.4 标为可选，本项只做队列
持久化 + e2e）。
