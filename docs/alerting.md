# P3 上报闭环（策略门 + Alert 生命周期 + SaaS）

> 把架构蓝图 §14「候选事件关联与正式上报」做成**确定性、幂等、可审计**的闭环。
> 前置：P0 契约（`../schemas/saas-alert-event-v1.json`）+ Agent 调查结论 + P2 候选。

---

## 1. 一句话

**Agent 的调查结论 → 确定性策略门判定是否上报 → 正式事件（同一 Episode 复用）→ 幂等投递 SaaS → 人工反馈回填。**

```bash
python -m data_dog.alerting.p3_demo          # 人读输出
python -m data_dog.alerting.p3_demo --json   # 机器可读
pytest tests/test_alerting.py -q             # 20 项
```

实测输出（节选）：
```
【confirmed+严重码】✅ 上报   严重度=critical  revision=2
   命中条件: 条件①厂商严重错误码; 条件③confirmed+置信度; 条件④多中等级候选
【rejected】⛔ 不上报   抑制原因: rejected（误报排除）
【缺证据】⛔ 不上报     抑制原因: 证据引用缺失（失败关闭）
【同Episode更新】✅ 上报  event_id 不变，revision 2→3
SaaS 收到事件数：2（无重复投递）
```

---

## 2. 数据流

```
InvestigationReport + signals
   → ① ReportPolicyGate（§14.2 确定性决策：四条件 OR + 冲突消解 + 失败关闭）
   → ② AlertEvent 新建/更新（同一 Episode 复用 event_id，只递增 revision）
   → ③ AlertLifecycle（§14.3 状态机）
   → ④ AlertOutbox（§16.1 幂等投递 + 重试 + 死信）
   → ⑤ SaaSTestAdapter（接收 + Receipt + 反馈）
```

---

## 3. 模块

| 文件 | 职责 |
|---|---|
| `policy.py` | **确定性策略门**：§14.2 四条件 + severity 冲突消解（取最高）+ 证据门控 |
| `lifecycle.py` | **Alert 状态机**：Detected→Confirmed/Escalated/Recovering/Resolved/Closed；非法转移抛错 |
| `outbox.py` | **幂等 outbox** + 幂等键构成规则（缺口 E-7）；重试与死信 |
| `saas.py` | 测试 SaaS Adapter：幂等 Receipt、禁止字段剔除、反馈回填 |
| `service.py` | 端到端编排；Episode 管理 |
| `p3_demo.py` | 演示入口 |

---

## 4. 三个关键设计（面试能讲）

1. **确定性决策表，不让模型判断**（§4.1）：上报是副作用，必须可复现可回归。策略门把 §14.2 的 OR 条件实现为可穷举的判定，并补上原文没给的**冲突消解**（severity 取最高）。模型今天判 A 明天判 B 没法测试。
2. **Episode 语义**（§14.3）：同一逻辑故障复用**同一 event_id + 同一幂等键**，状态更新只递增 `event_revision`——避免 SaaS 收到一堆无关新事件。幂等键构成规则被定死（缺口 E-7）：`device_ref:event_type:episode_key`。
3. **失败关闭**：证据引用缺失 → 不上报；rejected（误报）→ 不上报；未知状态转移 → 抛错。绝不"大概齐"。

---

## 5. 安全 / 隐私

SaaS Adapter 投递前**剔除禁止字段**（`payload_ref` / `stack` / `secret` / `api_key` / `model_reasoning` 等，递归）——只发业务摘要 + 受控证据 Receipt（§12.5）。

---

## 6. 与架构条目对照

| 架构 | 状态 |
|---|---|
| §14.2 确定性上报策略（四条件） | ✅ `policy.py`（含冲突消解） |
| §14.3 Alert 生命周期 | ✅ `lifecycle.py` |
| §16.1 至少一次 + 幂等副作用 | ✅ `outbox.py`（幂等键规则定死） |
| §12.5 SaaS AlertEvent 契约 | ✅ 投递 payload 对齐（禁止字段剔除） |
| §21.1 测试 SaaS Adapter + 反馈 | ✅ `saas.py` |
| §19.3 端到端回放验收 | 🟡 P1+P2+P3 各段已通；跨段一条命令的整链回放待补 |
| 通知/值班/升级 SLA | ❌ 未做（运营层，属 P4/文档） |
