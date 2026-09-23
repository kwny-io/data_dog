# Agent 降级路径（§9.4）

> 代码：`src/data_dog/agent/degradation.py`　演示：`python -m data_dog.agent.degradation_demo`
> 测试：`tests/test_degradation.py`（12 case）

## 解决什么问题

D 现有链路默认 **Agent 在线**：候选 → `AgentLoop.investigate` → 报告 → `ReportPolicyGate`（§14.2）决定上不上报。

但 §9.4 要求：**Agent 不可用时热路径不能停摆**。具体三条约束：

1. 严重问题**必须照样上报**，不能因为模型挂了就漏报；
2. 普通候选**不能丢**，要留到 Agent 恢复后复核；
3. 队列**不能无限堆积**，否则内存/磁盘被撑爆。

本模块把这三条做成**确定性决策表**——纯函数、零外部依赖、可穷举可测试。

## 决策表

| 条件 | 路径 | 说明 |
|---|---|---|
| 命中严重硬规则 | `direct_report` | 绕过 Agent，确定性上报（fail-safe） |
| Agent 不可用 + 队列未满 | `queue_for_agent` | 进有界队列等恢复 |
| Agent 不可用 + 队列已满 | `direct_report` | 退化为直报，只保聚合证据，不堆积 |
| Agent 可用 | `investigate_now` | 正常交付调查 |

**硬规则优先于可用性**：即使 Agent 在线，命中硬规则的候选也走直报（不等调查、不占算力）。

硬规则判据（`is_hard_rule`）：`severity_hint == "critical"`，或信号/规则名中含
`OVERTEMP / E_STALL / STALL / OVERVOLT / UNDERVOLT / EMERGENCY / FAULT / SHUTDOWN / TRIP / SAFETY`。

> 判据刻意用**明确的错误码特征**，不用过宽模式（如单字符 `E_`），避免把正常信号误伤成直报。
> 该函数公开导出，供 `alerting/policy.py` 与评估侧复用同一份判据，防止两处各写一份阈值导致语义漂移。

## 与 §14.2 策略门的分工

```
候选 ──[degradation §9.4]──> 该不该进调查 ──> AgentLoop ──[policy §14.2]──> 该不该上报
             上游分流                                        下游门控
```

两者**互补，不重叠**：degradation 管"调查之前 Agent 挂了怎么办"，policy 管"调查完了该不该上报"。

## 用法

```python
from data_dog.agent.degradation import DegradationPolicy

policy = DegradationPolicy(agent_available=True, queue_capacity=100)
for cand in candidates:
    d = policy.decide(cand)
    if d.path == "investigate_now":
        report = agent.investigate(cand)
    elif d.path == "queue_for_agent":
        pass                       # 已入队，等 Agent 恢复
    else:                          # direct_report
        svc.process(aggregate_evidence(cand), ...)

# Agent 恢复后取回排队候选
for cid in policy.drain():
    ...
```

批量：`policy.decide_batch(cands)`；查看队列：`policy.queued`；清空：`policy.reset()`。

## 边界与未做

- **只做决策，不做传输**。队列是进程内 `list`。接真实 MQ（Kafka / NATS / Redis Stream）
  时替换 `_queue` 即可，**决策逻辑不变**——这正是把它独立成纯模块的原因。
- **队列非持久**。进程重启后队列丢失；真实部署需落盘或走 MQ（属 §9.4 后期/大工程部分）。
- **未接进 `e2e/replay.py`**。当前是独立模块 + 独立 demo，e2e 主链路未改动
  （保持既有测试零回归）。
- `DECISION` 不含"重试/退避"语义——退避属于传输层，不在决策表范围内。

## 设计依据

本模块是架构 §9.4「Agent 不可用时的降级」与 §16.4「失败关闭与失败开放」的
确定性落地：三条处置路径（硬规则直报 / 有界排队 / 正常调查）逐条对应 §9.4
所列约束，队列超限保留聚合证据对应「不无限堆积原始日志」。
