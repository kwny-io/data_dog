# 架构总览

> 本文面向接手者：**代码分几层、每层做什么、依赖朝哪个方向流、数据从哪来到哪去。**
> 各子系统细节见同目录其余文档；本文只讲全局与边界。

---

## 1. 一句话

一个**故障调查 Agent**：把机器狗的原始日志变成一份可上报、可追责、可复现的结构化结论。
整链分四段——**P1 离线算子工厂 / P2 在线数据面 / Agent 受控调查 / P3 确定性上报**——
链上**热路径零 LLM**（接入→检测→候选全程确定性），LLM 只出现在"写调查报告"那一步。

---

## 2. 分层

```
┌─────────────────────────────────────────────────────────────┐
│ 接入 / 交付层   service/ (FastAPI)   stream/ (流式源)  ui/   cli.py │
├─────────────────────────────────────────────────────────────┤
│ 编排层          e2e/  (整链回放)                              │
├─────────────────────────────────────────────────────────────┤
│ 业务段层                                                     │
│   P1 operator_factory/   文档→知识→算子→回放验证              │
│   P2 data_plane/         网关→兼容绑定→解析→检测→候选         │
│   Agent  agent/ + tools/ + evidence/ + providers/            │
│   P3 alerting/           策略门→事件生命周期→幂等出箱→SaaS    │
├─────────────────────────────────────────────────────────────┤
│ 支撑层                                                       │
│   storage/ (持久化 + RLS)  state/ (共享状态: memory/pg/redis) │
│   correlation/  reporter/  synth/  mujoco                    │
│   knowledge / knowledge_ingest / eval_feedback               │
├─────────────────────────────────────────────────────────────┤
│ 横切层  core/ (日志/指标/追踪)  settings.py  errors.py        │
│         context.py  paths.py  observability.py  schemas/     │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. 依赖方向（单向，无环）

```
                    cli.py / service/ / ui/
                            │
                          e2e/
                            │
        ┌──────────┬────────┴────────┬──────────┐
     P1           P2              Agent          P3
  operator_    data_plane/     agent/ +        alerting/
  factory/     (无 LLM)        tools/ +        (无 LLM)
     │            │            evidence/          │
     └────────────┴────────────┴─────────────────┘
                            │
                         storage/
                            │
        ┌───────────────────┴───────────────────┐
   core/ settings paths errors context schemas observability
```

**守则**：业务段只向下依赖支撑层与横切层；段与段之间**不横向 import**——
它们靠 **e2e/ 编排**与**共享的事件模型**（`data_plane/events.py`、`schemas/`）连接。
好处是 P1/P2/P3 可各自单测，不牵扯整链。

例外（有意的、最小的耦合）：
- `evidence/detectors.py` 用 P1 产出的算子在线上跑检测——这是 **P1→P2 的接缝**，属设计。
- `data_plane/scenarios.py` 与 `presentation.py` 的 `verdict` 派生被 `ui/` 直接 import——
  UI 不改，故 `verdict` 走**委托**而非替换。

---

## 4. 数据流（一条遥测的历史）

```
原始日志
  │  POST /v1/telemetry   （身份只认请求头 X-Tenant-Id/X-Device-Sn）
  ▼
gateway.py        网关校验：字段完整性、协议版本、体积上限、身份注入
  ▼
resolver.py       兼容绑定：vendor/firmware/protocol → 算子集（拿不准就拒 = 失败关闭）
  ▼
chain.py          解析归一：原始 payload → NormalizedTelemetryEvent（数值带出处）
  ▼
detectors.py      在线检测：跑 P1 算子 → 命中
  ▼
candidate.py      候选构建：按 (设备,部件,信号) 合并命中 → CandidateEvent
  ▼
storage/          候选落库（(tenant_id, candidate_id) 复合主键，幂等 upsert）
  │  POST /v1/investigations  {candidate_id}   ← 热路径到此为止，下面是"调查"
  ▼
AgentLoop         受控调查：只读工具查遥测/厂商知识/邻居/变更 → 假设 → 反证 → 报告
  ▼
reporter.py       提出上报**意图**（本地 outbox + 幂等 + Receipt）
  ▼
alerting/policy   确定性策略门：四条件决策表 + 冲突消解（不让模型判断上报）
  ▼
alerting/lifecycle  正式事件：同一 Episode 复用 event_id，revision 递增
  ▼
alerting/outbox   幂等出箱：(tenant_id, 幂等键, revision) 复合主键强制去重
  ▼
saas.py           测试 SaaS Adapter：投递（剔除禁止字段）
  ▼
人工反馈           POST /v1/feedback → 驱动告警状态机
  ▼
eval_feedback.py  回灌：人工标签 vs Agent 处置 → 一致率 / 误报率 / 漏报率
```

**两个接缝要记牢**：
1. **热路径的尽头是"候选落库"**。`/v1/telemetry` 绝不调 LLM，也绝不触发调查。
2. **上报由策略门决定，不由模型决定**。模型只产出调查报告，上不上报是确定性决策表说了算。

---

## 5. 关键设计决策（各自为什么）

| 决策 | 为什么 | 代码 |
|---|---|---|
| **确定性 ID 派生** | 幂等与可复现的基础：`cand_<hash(...)>`、`event_id`、`幂等键` 全由输入唯一确定 | `alerting/outbox.py` |
| **租户进复合主键** | 确定性 ID 会让两租户撞同一 PK、静默覆盖他人数据——加列过滤修不掉 | `storage/models.py` |
| **受限 DSL，不 eval** | 厂商字符串不能当代码执行；自建词法→AST 解释器，函数闭集白名单 | `operator_factory/dsl.py` |
| **失败关闭** | 身份只在网关注入、兼容绑定拿不准就拒、错误收敛为有限错误码 | `data_plane/`、`errors.py` |
| **证据引用不复制** | 结论挂**出处**（ref）而非内嵌原文；数值只用有出处的 | `evidence/refs.py` |
| **诚实抽取** | 官方写 90°C→`verified`；无出处的 75°C→`unconfirmed`，不冒充厂商事实 | `operator_factory/knowledge.py` |
| **correlation-id 而非完整追踪** | 无 OpenTelemetry SDK；只做 W3C traceparent 传播 | `core/tracing.py` |

---

## 6. 存储模型

全部租户作用域（`tenant_id` 进主键），**新增的表都启用 RLS**（策略口径见 `docs/storage.md`；
后台跨租户扫描需走 `session_scope(system=True)`）。详见 `docs/storage.md`。

| 表 | 主键 | 来源 |
|---|---|---|
| `telemetry_events` | `telemetry_id` | P2 |
| `candidates` | `(tenant_id, candidate_id)` | P2 |
| `reports` | `(tenant_id, investigation_id)` | Agent |
| `alerts` | `(tenant_id, event_id)` | P3 |
| `alert_outbox` | `(tenant_id, idempotency_key, revision)` | P3 |
| `alert_feedback` | `(tenant_id, id)` | P3 |
| `knowledge_documents` | `(tenant_id, document_id)` | A7 |
| `knowledge_chunks` | `(tenant_id, chunk_id)` | A7 |
| `investigation_job` | `(tenant_id, job_id)` | G1（异步调查） |
| `oncall_rota` / `alert_delivery` | `(tenant_id, …)` | P4-3（值班/投递台账） |
| `operator_registry` / `operator_rollout_event` | `(tenant_id, …)` | P4-2（控制面/灰度台账） |
| `change_records` | `(tenant_id, change_id)` | G7（变更源） |
| `state_kv` / `state_checkpoint` | `key` / `(tenant_id, checkpoint_id)` | G3（共享状态，**无租户维度**） |
| `audit_log` | `(tenant_id, audit_id)` | **A1（安全审计，只增不改）** |

迁移由 alembic 版本化（`alembic/versions/`），启动走 `upgrade_head`。

---

## 7. 可观测性

- **日志**：JSON 结构化（`core/logging.py`），带 `request_id` / `trace_id` / `tenant_id`。
- **指标**：Prometheus 文本（`core/metrics.py`），`/metrics` 暴露，**全局口径不分租户**（避免 label 基数爆炸）。
- **探针**：`/healthz` 存活（不查依赖）、`/readyz` 就绪（数据库不通 → 503）。

---

## 8. 诚实边界（不宣称做不到的）

三条曾经的缺口现已补齐，**但都有条件**，条件同样要讲清楚：

- **真实遥测流：已支持，默认关**。`stream/` 提供文件尾随 / HTTP 推送 / MQTT / Kafka
  四种传输，走与 HTTP 入口同一条数据面（见第 4 节的 `POST /v1/telemetry` 路径）。
  默认 `DD_STREAM_ENABLED=0`——演示/CI 无真实设备，**不是做不到**。评估集证据仍来自
  mock / 合成 / MuJoCo，故"真实误报率"须真实数据回灌方能定论。
- **多租户：应用层行隔离 + PostgreSQL RLS 双保险**。应用层：`tenant_id` 进复合主键、
  仓储强制过滤、跨租户读 404。数据库层：迁移 `a7b8c9d0e1f2` 对租户表启用
  `ROW LEVEL SECURITY`。**RLS 仅 PostgreSQL**，SQLite 无 RLS（本地开发靠应用层）；
  且 **superuser / 表属主默认绕过 RLS** → 生产连接须用非属主、非 superuser 角色。
- **水平扩展：有状态算子已外置**。`StateStore.mutate(key, fn)` 原子 RMW 原语，
  后端 `memory` / `postgres` / `redis` 可插拔（`DD_STATE_BACKEND`）。候选归并与限流桶
  都在共享后端，多副本一致。**流式源多副本消费已按源协调**（各源 `coordination()`，
  `/readyz` 可见）：Kafka 用消费者组；MQTT 配 `DD_MQTT_SHARE_GROUP` 走共享订阅
  `$share/<group>/<topic>`（broker 轮询投递，一条消息只给一个副本）；file 用应用层
  **租约选主**（`DD_STREAM_SHARE_OFFSET` 默认开）——同一时刻只有一个副本读该文件，
  持有者崩溃后由共享偏移续读。**边界**：file 源本质仍是**单消费者**（租约只"选一个"，
  要并行须转 Kafka/MQTT）；租约依赖各副本时钟大致同步（须跑 NTP）；租约接管有 ≤TTL 窗口。
- **调查端点已可异步，默认仍同步**。`DD_INVESTIGATE_ASYNC=1` 时 `POST /v1/investigations`
  提交即返回 `202 {job_id}`，真实 LLM 调查交给进程内 worker 池（`DD_INVESTIGATE_WORKERS`），
  worker 立即释放；`GET /v1/investigations/jobs/{job_id}` 轮询、`.../cancel` 取消。
  多副本不重复执行靠**两层**：共享待办队列 + 数据库条件领取（`UPDATE ... WHERE status='pending'`）。
  **默认关**（`=0`）——既有前端按同步长任务实现，无预警翻成 202 会直接崩；`?sync=1` 强制走同步。
  **边界**：进程内线程池**不是**分布式调度器；取消是协作式（等当前步返回）；job 表需保留策略。
- **P4 大工程八项已落地**（2026-09-29 重估后）。沙箱算子运行时、控制面（Registry/Canary/回滚/签名）、K8s/Helm 编排、车队级分片、跨地域多活（应用层）、退化趋势预测、通知/值班/升级 SLA、软实时基准——均见 `docs/p4/`，各带诚实边界（硬实时 <1ms 仍为非目标；多活 RPO>0；沙箱 Windows 无 seccomp；分片再平衡双读未实现）。**当年标"不做"是因技术未到位，非真不该做**。

---

## 9. 文档地图

| 想了解 | 看 |
|---|---|
| 落地交付（服务怎么跑、Docker） | `docs/delivery.md` |
| P1 算子工厂 | `docs/operator_factory.md`、`docs/dsl_spec.md` |
| P2 数据面 | `docs/data_plane.md` |
| P3 上报闭环 | `docs/alerting.md` |
| Agent 降级 | `docs/degradation.md` |
| 持久化 | `docs/storage.md` |
| 整链回放 | `docs/e2e.md` |
| SLO 量化目标 | `docs/slo.md` |
| 事件 Schema | `schemas/README.md` |
| MuJoCo 数据源 | `docs/mujoco_sim.md` |
