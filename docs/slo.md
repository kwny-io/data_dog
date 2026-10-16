# data_dog 首期量化目标（SLO v0.1）

> P0-2 交付物。对齐缺口 **A-4 / E-2**（"全文无数字，无法选型、无法验收"）。
> **诚实声明**：本文所有数字都是**设计目标 / 预算值**，不是实测值。每项都给了**推导过程**与**假设来源**，便于拿到真实设备数据后校准（见 §5）。**未校准前不得对外宣称"系统支持 X"**。

---

## 0. 怎么用这份文档

- **选型依据**：P2 在线数据面（§9）选不选 Kafka/Flink、算子用什么运行时，以本表数字为门槛。
- **验收依据**：P2/P3 的验收必须逐条对照本表（有目标才能判 pass/fail）。
- **校准对象**：拿到硬件团队的真实采样率/峰值日志量后，回填 §5 并更新此表版本。

---

## 1. 假设（给定条件）

首期范围 = **单个组件**（架构 §21：关节电机或电池二选一），单设备、单租户起步。

| 假设 | 取值 | 依据 / 备注 |
|---|---|---|
| 单设备被测组件数 | 12 个关节电机（或 1 个 BMS） | Go2 有 12 个关节（4 腿 × 髋/大腿/小腿） |
| 单组件遥测频率 | 10 Hz（规范化后） | 假设本体已聚合；原始逐帧日志频率更高，在网关侧就压掉 |
| 单条规范化事件大小 | ≈ 0.5 KB | 见 §12.2 字段量级（JSON，含 provenance） |
| 单条原始日志信封大小 | ≈ 2 KB（含 ref，不含正文全文） | 大正文经 `payload_ref` 外置（§12.1） |
| 在线设备数（首期） | 1 ~ 10 台 | MVP 规模，非车队 |
| 被测组件发生真实故障频率 | ≤ 1 次/设备/天 | 用于反推候选/告警率上限 |

---

## 2. 吞吐目标

> 公式：**速率 = 组件数 × 每组件频率 × 设备数**

| 指标 | 目标值（首期） | 推导 |
|---|---|---|
| 单设备规范化事件速率 | **120 事件/秒** | 12 关节 × 10 Hz = 120 |
| 单设备原始信封速率 | **≤ 1,000 条/秒** | 原始逐帧更密；网关前置聚合/限流到此量级 |
| 集群总入口（10 台设备） | **≤ 12,000 事件/秒** | 120 × 10 × 10 = 12,000 |
| 单设备数据率（规范化） | **≈ 60 KB/秒**（0.5 KB × 120） | 用于估带宽/存储 |
| 突发系数 | **×3，持续 ≤ 60 秒** | 工况切换/重连补传的短时尖峰 |

**选型含义**：单设备 120 事件/秒、集群 ~1.2 万/秒 属**中低吞吐**——
→ **不需要** Flink/Kafka 级设施，单机流处理 + 有界队列即可（这直接支持架构 §23"先验契约、压测再选型"）。
→ 若后续设备数 >100 台或频率 >100 Hz，需重新评估（触发条件见 §5）。

---

## 3. 延迟目标（端到端预算）

从"日志进入网关"到"候选异常产出"的**热路径**时延预算（不含 Agent，Agent 属慢路径）：

| 阶段 | 预算 | 说明 |
|---|---|---|
| 接入 + 前置校验 | ≤ 10 ms | 大小/编码/签名检查 |
| 兼容解析（Parser/Normalizer） | ≤ 20 ms | 单条算子执行上限，对齐 §10.2 `timeoutMs: 20` |
| 窗口特征（Window，有状态） | ≤ 50 ms | 取决于窗口大小，含状态读写 |
| 检测（Detector） | ≤ 20 ms | 规则/趋势 |
| **候选产出端到端 P95** | **≤ 150 ms** | 10+20+50+20 之和 + 余量 |
| **候选产出端到端 P99** | **≤ 500 ms** | 容忍 GC/重连抖动 |

**关键约束**：热路径**不调用大模型**（§4.1）；上表预算内**不允许**任何 LLM 调用。

**实测对照（P4-8，2026-09-29）**：上表预算只覆盖**计算路径**（校验→解析→窗口→检测），
而端到端 P99 实测**由落库排队主导**。PostgreSQL 16 形态实测：

- **低负载（4 workers @ 20 rps）：写入 P99 133ms ≤ 500ms ✅ 达标**——单请求开销在预算内。
- **中负载（60 workers @ 300 rps）：写入 P99 1504ms ❌ 超标 3×**——同步落库在并发下排队，
  尾部延迟被等待时间拉高（吞吐 89 req/s 仍能处理，但延迟不平稳）。
- 读取（32 workers @ 150 rps）：P99 237ms ✅ 达标。
- 对照：SQLite 形态写入 P99 6.3s——**换 PG 改善 4×，但未消除中负载排队**。

**T1/T1.5 已实施后的复测（2026-10-10，同机 A/B）**：连接池可配、SQLite WAL、候选索引
删除、**一条事件的全部命中合并成一次提交**（`StateStore.mutate_many`）都已落地，
**每请求事务数实测 9.16 → 7.13（−22%）**——但 **P99 3124 → 3078 ms，没动**。
另测 `DD_SERVICE_WORKERS` 1/2/4/8：**2 个 worker 就饱和（~70 req/s）**，
最优 P99 **1298 ms（4 workers）**，仍超标 2.6×。

**根因（已三次修正，别再沿用旧说法）**：不是"连接池饱和"（§2.1 已推翻），
**不是"提交吞吐"**（§6 已推翻——7.13 × 36 = 257 事务/s，远低于本机 513/s 上限，
且压测中 PG 31 个后端**全部 `ClientRead`**），**也不是"每请求往返次数 × 单次往返成本"**
（§7 已推翻——DB 往返通道实测 **4208~7795 次/秒**，而 `12.1 条 × 300 rps = 3630` 落在容量内；
loadgen 客户端实测能推 **997 req/s**，故客户端也不是瓶颈）。

**当前状态**：**仍未定论**，但已把三个错误答案排除干净，方向收窄到**服务进程自身的 CPU**
（PG 形态下真身进程在 60 并发实测吃 **137% 单核**）。⚠️ 而 §6 那批吞吐/扩展数字
**因测量装置缺陷（残留服务占端口、PG 会话残留导致新实例启动挂死）已全部作废**——
**引用本文任何吞吐数字前先读 `docs/write_path_batching.md` §7.4。**

**结论**：P99 ≤ 500ms **低负载与轻并发达标**（8 workers @40 rps 实测 404.6 ms）、
**中负载未达标**；未达标源于**每请求往返次数**，非换 DB、非调参、非加进程可解
（加 worker 在 2 就饱和）。详见 [`docs/write_path_batching.md`](write_path_batching.md) §6 与
[`benchmarks/baseline.md`](../benchmarks/baseline.md)。
**在目标生产环境 + 真实负载复测之前，本表 P99 目标不得对外宣称已达标。**

---

## 4. 算子与状态目标

| 指标 | 目标值 | 依据 |
|---|---|---|
| 单条算子 CPU 时间 | ≤ 10 ms | §10.2 `cpuMillisPerMessage: 10` |
| 单条算子内存 | ≤ 32 MB | §10.2 `memoryMb: 32` |
| 单条算子输出上限 | ≤ 16 KB | §10.2 `maxOutputBytes` |
| 单算子超时 | 20 ms（P95 路径）/ 200 ms（硬上限） | 超时即熔断回退稳定版（§16.3） |
| 有状态算子 checkpoint 大小 | ≤ 1 MB/算子 | 窗口状态，随窗口数线性 |
| checkpoint 间隔 | 5 秒 或 1000 条（先到为准） | 平衡恢复时间与开销 |
| 故障恢复时间（RTO） | ≤ 30 秒 | 从 checkpoint 恢复，不重放全量 |
| 状态保留（可回放窗口） | ≥ 24 小时 | 覆盖"同 Episode 复发"判断所需 |

---

## 5. 质量与业务目标

| 指标 | 目标值 | 说明 |
|---|---|---|
| 解析成功率 | ≥ 99% | 已知模板；未知模板单列，不计失败 |
| 未知模板率 | ≤ 1% 且持续下降 | 触发"适配退化"告警的观察项 |
| 候选事件生成率 | ≤ 5 候选/设备/小时 | 过高说明阈值过松（§18.1） |
| 候选→告警转化率 | 10% ~ 40% | 太低=误报多；太高=漏报风险 |
| 漏报率（在有标签集上） | ≤ 5% | §18.3 故障覆盖率 |
| Agent 调查时延 | P95 ≤ 10 秒 | 慢路径，允许调 LLM。**实测（推理模型，n=18）：P50 36.5s / P95 59.5s / P99 75.7s —— 未达标 ≈6×**，见 [agent_cost_latency.md](agent_cost_latency.md) |
| 单次调查成本 | 目标 ≤ ¥0.05（可配上限 ¥0.20） | 对应缺口 E-10；超限则降级/转人工。**实测达标**：P99 ¥0.044（参考价 0.27/1.10 USD/1M）、超上限率 0，见 [agent_cost_latency.md](agent_cost_latency.md) |
| 端到端（异常→正式事件） | P95 ≤ 15 秒 | 含 Agent + 策略门 + Outbox |

---

## 6. 可靠性与保留

| 指标 | 目标值 | 依据 |
|---|---|---|
| 投递语义 | 至少一次 + 幂等副作用 | §16.1 |
| 消息丢失率（网关→候选） | ≤ 0.01% | 有界队列 + 重传 |
| Outbox 投递成功率 | ≥ 99.9%（含重试） | §16.1 |
| 重复告警抑制 | 同 Episode 不产生新 event_id，只递增 revision | §14.3 |
| 原始日志保留 | 默认 7 天（可配置，按租户合同） | §15；**不设"永久保存"默认**。**已落地**：`data_dog.retention`（`make retention`）按 `DD_RETENTION_RAW_DAYS` 有界分批清理 |
| 规范化指标保留 | 90 天 | 时间序列。**已落地**：`DD_RETENTION_CANDIDATE_DAYS`（对派生候选表） |
| 证据包保留 | 与对应告警同生命周期 | Evidence Store。告警台账与报告**默认不删**（`DD_RETENTION_ALERT_DAYS` 默认 0），要清须显式开启 |
| 调查运行日志保留 | 默认 90 天 | 派生执行记录（ReAct 轨迹/失败上下文）。**已落地**：`DD_RETENTION_RUN_LOG_DAYS`（设 0 = 永久）。删了**不能复原**（重跑得到的轨迹与当时不同），但不跟 job 的 7 天——要够事故复盘。见 [run_log.md](run_log.md) |
| 审计记录保留 | ≥ 1 年 | §17；**已落地**：`audit_log` 表 + `GET /v1/audit`（A1）。写入只发生在动作发生处；**清理默认关闭**（`DD_RETENTION_AUDIT_DAYS` 默认 0）——销毁审计记录本身是合规风险，要删必须由人显式开启；一旦删除，动作本身写 `retention.purge` 审计留痕 |

> **保留策略的执行口径（④）**：清理是**破坏性**动作，所以默认值按"删了能不能复原"分档——
> 可重灌的（原始日志 7 天、派生候选 90 天、已终态调查 job 7 天）**默认执行**；
> 不可复原的业务/合规台账（告警+报告+投递台账、安全审计）**默认 0 = 不删**。
> 执行时**有界分批**（每批短事务、批数用满即停并标记 `capped`，剩余下一轮继续），
> 跨租户走 `session_scope(system=True)`（否则 PG 上 RLS 会让它**静默删 0 行**）。
> 保留判据用 **ingestion 时间**（`created_at`/`ts`/`finished_at`），不用事件时间——
> 否则一条补传的历史时间戳会让该行永远"年轻"、永远清不掉。

---

## 7. 明确不承诺（Non-SLO）

为避免夸大，以下**首期不设目标、也不对外宣称**。其中三项已**重新评估并转入 P4 规划**
（见 `docs/p4/`）——当初标"不做"多因技术未到位，非真不该做：

- ⏳ **精确 RUL（剩余寿命）**：**仍不承诺研究级精确 RUL**；但**趋势外推的 ETA + 区间**
  已转入 P4-7 规划（输出"预计 N 小时后到阈值 ± 区间"，非确切故障日期）。
- ⏳ **车队级（>100 台）高吞吐**：转入 P4-5 规划——**吞吐以实测为准**（`tools/loadgen.py`），
  不空口保证具体数字。
- ⏳ **跨地域多活**：转入 P4-6 规划，但定位**异步多活（RPO>0）**；
  **RPO=0 仍不承诺**——需数据库同步复制设施，非应用代码可解。
- ❌ **硬实时（<1 ms）控制回路**：**如实保留为非目标**。Python + FastAPI + 关系库
  往返**物理上做不到 <1ms**——这是语言/架构的真实约束。改述为**软实时**：热路径
  P99 有界（目标 ≤500ms），由 P4-8 实测基线支撑。

---

## 8. 待真实数据校准项（必须先问硬件团队）

| 编号 | 待确认 | 影响 |
|---|---|---|
| S-1 | 本体实际上传协议、峰值吞吐、单条大小 | §2 全部吞吐数字 |
| S-2 | 实际采样频率（原始 & 聚合后） | §2 / §4 |
| S-3 | 离线时长与断线补传上限 | 突发系数、保留期 |
| S-4 | 迟到数据上限（时间水位线） | §3 延迟、§16.2 |
| S-5 | 是否已有历史故障/维修/误报标注 | §5 漏报率能否验收 |

> 校准后：更新本文件，版本递增为 v0.2，并在缺口台账 E-2 标注"已校准"。

---

## 9. 告警规则（把目标变成"会响的"）

> **为什么有这一节**：`servicemonitor.yaml` 只解决"抓得到"。指标再全，没有一条规则把目标
> 变成"会响的"，SLO 就只活在文档里——`data_dog_audit_write_failed_total` 这类注释写着
> "必须告警"的计数器，此前也只是空头承诺。规则本体在
> [`helm/data-dog/templates/prometheusrule.yaml`](../helm/data-dog/templates/prometheusrule.yaml)
> （`prometheusRule.enabled=true` 才渲染；前提同 ServiceMonitor：集群装了 Prometheus Operator）。
> **本表与规则文件是同一件事的两面——改一边必须改另一边。**

| 规则 | 指标 / 口径 | 触发 | 级别 | 依据 |
|---|---|---|---|---|
| `DataDogApiErrorRateHigh` | `data_dog_http_requests_total{status=~"5.."}` / 全部请求 | > 1% 持续 10m | critical | **工程阈值**：本仓**没有**可用性百分比目标（§7 首期不承诺），1% 是"抢在用户感知前"的线 |
| `DataDogIngestLatencyP99High` | `data_dog_http_request_duration_seconds_bucket{path="/v1/telemetry"}` 的 P99 | > 500ms 持续 15m | warning | **本文件 §3**：候选产出端到端 P99 ≤ 500ms。只算热路径——同步调查等真 LLM（10–60s），混进来会把 P99 带偏 |
| `DataDogTargetDown` | `up{job=~".*data-dog.*"}` | = 0 持续 5m | critical | 抓取链路可用性（Prometheus 侧） |
| `DataDogAuthDisabled` | `data_dog_auth_enabled` | = 0 持续 5m | critical | README 诚实边界："端口开放 = 任何人可读写" |
| `DataDogInvestigationQueueBacklog` | `data_dog_investigation_queue_depth` | > `prometheusRule.queueDepthThreshold`（默认 50）持续 15m | warning | **工程阈值**：队列**无长度上限**是有意的失败开放，积压只能靠它被发现 |
| `DataDogInvestigationFailures` | `data_dog_investigation_jobs_total{status="failed"}` 的 30m 增量 | > `prometheusRule.failureCountThreshold`（默认 5） | warning | 失败**会 fail-closed 到人工**（不是静默），但仍需要量级可见性 |
| `DataDogLlmBudgetExceeded` | `data_dog_llm_budget_exceeded_total` 的 15m 增量 | > 0 | warning | A1 额度硬熔断。指标**故意不带租户标签**（基数）：知道"有人撞线"，不知道是谁 |
| `DataDogAuditWriteFailed` | `data_dog_audit_write_failed_total` 的 10m 增量 | > 0 | critical | §6/§17：审计只增；**写失败不会让业务请求失败**，这是唯一可见窗口 |
| `DataDogWriteBehindDataLoss` | `data_dog_write_behind_total{result=~"failed\|rejected_full"}` 的 10m 增量 | > 0 | critical | 开了 write-behind 时"响应已 200 但没落库"的证据（静默丢数据） |
| `DataDogStreamDown` | `max(data_dog_stream_up)` | = 0 持续 5m | critical | 仅当 `stream.enabled=true` 才渲染（否则 API 的 `stream_up=0` 会永久误报） |
| `DataDogStreamDropped` | `data_dog_stream_dropped_total` 的 10m 增量 | > 0 | warning | 背压证据：丢的是**消息**，不是延迟 |
| `DataDogReplicationLagHigh` | `max by (from,to) (data_dog_replication_lag_seconds)` | > 60s 持续 10m | warning | §7：跨地域多活定位 **RPO>0**；单区域没有该 series，规则自然不触发 |
| `DataDogPartitionHorizonLow` | `min(data_dog_partition_end_timestamp_seconds{kind="future"}) - time()` | < `prometheusRule.partitionHorizonSeconds`（默认 14 天）持续 10m | critical | A9 分区表：跨月没有分区 → 写入直接报 `no partition of relation ... found for row`。用**时间戳减 time()**，避免 Gauge 陈旧导致规则失灵 |
| `DataDogPartitionHorizonUnknown` | `absent(data_dog_partition_end_timestamp_seconds{kind="future"})` | 持续 1h | warning | 该组只在 `partition.enabled=true` 时渲染；此时**没有样本**会让上面那条变成死规则（Prometheus 对空向量求值不报错）。比"有效期不足"更早的警报 |
| `DataDogLlmLatencyP95High` | `histogram_quantile(0.95, sum by (le)(rate(data_dog_llm_request_duration_seconds_bucket[10m])))` | > `prometheusRule.llmLatencyP95Seconds`（默认 60s）持续 15m | warning | **工程阈值**：同步调查要等一次真 LLM；失败/超时**也计入**，只统计成功会把 P95 低估 |
| `DataDogLlmFailureRateHigh` | 超时+不可用 的 rate / 全部 rate（clamp_min 防分母 0） | > `prometheusRule.llmFailureRate`（默认 5%）持续 15m | warning | 失败**fail-closed**（降级为 needs_human_review）→ 这条等于"人在替模型兜底" |
| `DataDogOutboxBacklog` | `max(data_dog_db_rows{table="outbox_pending"})` | > `prometheusRule.outboxBacklogThreshold`（默认 100）持续 15m | warning | 告警产生了但没送出去；与 `DataDogAuditWriteFailed` 互补（一个写不进、一个送不出） |
| `DataDogEvidenceResolveFailed` | `increase(data_dog_evidence_resolve_total{outcome="execution_failed"}[15m])` | > 0 | warning | 数据源没配好/不可达（如 rawlog 未配置）。此前只表现为"用户打不开原始日志" |
| `DataDogEvidenceDeniedSpike` | `increase(data_dog_evidence_resolve_total{outcome="denied"}[15m])` | > `prometheusRule.evidenceDeniedThreshold`（默认 20） | warning | 正常应为 0；涨起来=越权探测信号。定位靠审计（指标刻意不带租户标签） |
| `DataDogPartitionDdlSkipped` | `increase(data_dog_partition_ddl_skipped_total[24h])` | > 0 | warning | A9 互斥：滚动发布时的少量 `lease_held` 属预期；**持续**出现说明持有者卡在长 DDL 或崩了没释放。`convert_busy` = 库层咨询锁被另一个转换占着（有意**不排队**，见 storage.md §8.3.1） |
| `DataDogPartitionDdlLeaseStuck` | `max(data_dog_partition_ddl_lease_age_seconds)` | > 1800s（2× 默认 TTL）持续 5m | warning | 租约被一个进程占着超过 30 分钟：要么长 DDL 卡住、要么持有者死了但租约还没过期——这期间**没人会补分区**。0（无主）不会触发 |
| `DataDogPartitionDdlFailed` | `max by (op) (data_dog_partition_ddl_consecutive_failures)` | > 0 持续 5m | warning | A9 **迁移失败**的可见窗口：CronJob 崩溃被 `backoffLimit` 重试后只留在 Job 日志里，指标上什么都不剩。用**连续失败数**而不是最后失败时间戳（后者被下一次成功挡住视线）。退出码 2（确认令牌/静默窗口/容量）与真异常计入，退出码 3（跳过）不计 |

**诚实边界**：

- **阈值不等于 SLO**：上表"依据"列把两者分开——只有接入 P99 直接来自本文件 §3 的预算，其余是
  **工程阈值**（防"文档没写、但线上确实会出事"的那类）。**没有任何一条规则能替代容量规划。**
- **规则不带租户标签**：所有表达式都是全局聚合（避免标签基数爆炸）。要定位"是哪个租户"，
  得查安全审计与调查运行日志（`GET /v1/audit`、`GET /v1/investigations/runs`），不在指标里。
- **分区组按部署形态渲染**：`DataDogPartitionHorizonLow/Unknown` 只在 `partition.enabled=true`
  的部署里渲染——没有分区表的部署（SQLite、未转换的普通表）根本没有这个样本，渲染出来就是永久误报。
- **渲染校验需要 `helm`**：本仓不内置 helm 二进制，`make helm-lint` / `make verify-chart`
  需要本机或 CI 安装（CI 由 `azure/setup-helm` 提供）。另有一条**不依赖 helm** 的 meta 测试
  [`tests/test_helm_alerts.py`](../tests/test_helm_alerts.py)：规则里出现的每个 `data_dog_*`
  指标必须在 `src/data_dog/core/metrics.py` 里真实注册——引用不存在的指标就是**永不触发的
  死规则**，这条测试专门钉它。

---

## 10. 版本记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-09-13 | 首版；全部为设计预算值，待真实数据校准 |
| v0.1.1 | 2026-10-14 | 新增 §9 告警规则：把 §3 的 P99 与两类"静默失败"（审计写入、write-behind 丢数）落成 PrometheusRule；版本记录顺延为 §10 |
| v0.1.2 | 2026-10-17 | §9 新增 `DataDogPartitionDdlFailed`：A9 分区迁移的**失败**此前不可见（只留在 CronJob 日志里） |
