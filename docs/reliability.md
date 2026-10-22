# data_dog 重试与超时审计（可靠性 v0.1）

> **交付物**：`src/**` 全量重试/超时点审计表 + 待改清单。
> **范围声明**：本文**只审计、不改任何实现代码**。审计对象是 `src` 下的重试环路、超时参数、
> 阻塞等待与退避策略；HTTP 层（`service/app.py` 路由）的**故障注入测试**不在本文范围（由
> service 拆分后的 HTTP 层测试补齐），但该文件里的超时预算已纳入审计。
> **基线**：`src/data_dog/state/redis.py` 的 `mutate()` 是本仓当前的规范样板（§1）。

---

## 0. TL;DR

- 审计 **23 个点**：**合规 16**、**无需改造 6**、**待改 0**（初版的 7 项待改 A~G 已全部落地）。
  「无需」的 6 条是明确豁免（见 §4），不是漏审。
- **已处理 8 项**（§3.3）：A=PG 三段超时（高）、F=变更源失败关闭（中）、
  B=流式源轮询预算（中）、C=outbox 投递预算（中）、D=LLM 客户端显式 `max_retries=0`（中）、
  E=MCP 看门狗/退避/总预算（中）、G=SMTP 超时独立配置项（低），
  以及外部已修的 H（redis `WatchError` 按类型判定，低）。
- **未修但已写明理由与触发条件的 2 项**（§3.2）：I=CMDB 拉取固定 5s 无重试（低）、
  J=`stop(wait=False)` 不等 worker（低）。
- **规范样板**是 `state/redis.py:123` 的 `mutate()`：次数上限 512 + 墙钟上限 5s +
  指数退避 × 抖动。本轮把同一口径推到了流式源轮询（B）、outbox 投递（C）与 MCP 客户端（E）。
- 大量 `while True` / `for _ in range(...)` **不是重试**（解析器、采样、交互、worker 主循环），
  已逐条排除，见 §4。

---

## 1. 规范：什么叫"次数 + 时间双上限 + 抖动退避"

样板：`src/data_dog/state/redis.py:123-151`（常量在 `:49-53`）

| 要素 | 样板取值 | 缺了它会怎样 |
|---|---|---|
| **次数上限** | `_RETRY_MAX_ATTEMPTS = 512` | 永久重试 = 调用方无限阻塞；慢环境（覆盖率插桩 / CI / GIL 争抢）下单次尝试可放大一个量级，纯墙钟预算会把"只是慢"误判为"失败" |
| **时间上限**（墙钟 deadline） | `_RETRY_DEADLINE_S = 5.0` | 重试次数够但每次都很慢时，最坏阻塞时间无界 |
| **抖动退避** | `min(0.5ms × 2^n, 3ms) × U(0.5, 1.5)` | 多写者同步重试 → 反复互相打断（实测 16 并发 100% 失败）；无抖动等于把活锁换成"整齐的惊群" |
| **失败即抛** | 预算耗尽抛 `RuntimeError`（带尝试次数） | 静默返回旧值/空值 = 静默丢更新（本仓最忌讳的失败模式） |

**判定口径**：一个点要判"合规"，必须同时说明三件事——重试**最多几次**、**多久之内**必须放弃、
两次尝试之间**怎么错开**。若该点根本**没有重试环**（单次调用 + 单次超时），则只需回答
"单次阻塞有没有上限"，不适用退避（表中记 `—`）。

---

## 2. 审计表（23 行）

图例：✅ 满足 · ⚠️ 部分/隐式 · ❌ 缺失 · — 不适用（无重试环）

| # | 位置（文件:行） | 当前策略 | 次数 + 时间双上限 + 抖动退避 | 结论 |
|---|---|---|---|---|
| 1 | `src/data_dog/state/redis.py:123-151`（常量 `:49-53`） | `mutate` 冲突重试：512 次 / 5s 双上限，指数退避（0.5ms→3ms）× `U(0.5,1.5)` 抖动，耗尽抛 `RuntimeError` | ✅ 512 次 · ✅ 5s · ✅ 指数+抖动 | **合规（样板）** |
| 2 | `src/data_dog/state/redis.py:158-169` | `keys()` 用 SCAN 游标循环分页（`count=500`）；不是重试环 | — | 无需 |
| 3 | `src/data_dog/state/pg.py:39-59` | `mutate` 用 `SELECT ... FOR UPDATE` 行锁串行化（无重试环）；引擎参数复用 `storage.db.engine_kwargs`，锁等待/建连均有上限 | — 无重试 · ✅ 复用的三段超时 · — | **合规**（已加固，§3.3-A） |
| 4 | `src/data_dog/storage/db.py:85-149`（配置项 `settings.py:112-133`） | `engine_kwargs`：PG 设 `connect_timeout=5s` / `pool_timeout=5s` / `statement_timeout=15s` + `pool_pre_ping`；SQLite 分支**不变** | — 无重试 · ✅ 5s / 5s / 15s（`DD_DB_*` 可调，0=不限语句） · — | **合规**（已加固，§3.3-A） |
| 5 | `src/data_dog/alerting/outbox.py:42-48,109-215` | `deliver()`：**单次调用内**按"次数 3 + 墙钟 2s（先到为准）"重试，间隔是抖动指数退避（0.05s→0.5s × `U(0.5,1.5)`）；耗尽进死信并记 `dead_reason`/`deadline_exceeded` | ✅ 3 次 · ✅ 2s · ✅ 指数+抖动 | **合规**（已加固，§3.3-C） |
| 6 | `src/data_dog/alerting/notifiers.py:134-149`（`_post_json`，调用点 `:165,:189`） | HTTP/IM 通知：单次 `urlopen(timeout=DD_NOTIFY_HTTP_TIMEOUT_S)`（默认 5s），不做重试，失败如实返回 `DeliveryResult(ok=False, detail=…)` | — 单次 · ✅ 5s · — | 合规 |
| 7 | `src/data_dog/alerting/notifiers.py:213-214` | 邮件通道 `smtplib.SMTP(..., timeout=DD_SMTP_TIMEOUT_S)`（默认 5s）；无重试 | — 单次 · ✅ 5s（**独立配置项**，不再复用 HTTP 的） · — | **合规**（已解耦，§3.3-G） |
| 8 | `src/data_dog/evidence/change_source.py:229-231,250-260` | CMDB 拉取：单次 `urlopen(timeout=self.timeout)`（默认 5s，可注入），**异常直接上抛**、不回空 | — 单次 · ✅ 5s · — | 合规 |
| 9 | `src/data_dog/providers/client.py:98-133`（默认值 `:52-57`） | LLM 调用：显式 `timeout=DD_LLM_TIMEOUT_S`（默认 60s）+ **显式 `max_retries=0`**——一次 `chat()` = 一次 HTTP 往返，重试预算归**我们自己的**调用方所有：`_DeadlineProvider`（总预算）+ `_RetryProvider`（有界重试，见第 24 行） | ✅ 1 次 + 外层 `DD_LLM_RETRIES` · ✅ 60s（外层 90s） · ✅ 抖动退避（见 24） | **合规**（已加固，§3.3-D；重试在 24 行） |
| 10 | `src/data_dog/service/app.py:1927-1939`（装配 `:1949-1968`） | `_DeadlineProvider` 给整次调查加 `DD_INVESTIGATE_TIMEOUT_S`（默认 90s）总预算，到点抛 `LLMCallError(TIMEOUT)` → Agent 失败关闭 | ✅（步数 `max_steps=8`）· ✅ 90s · — | 合规 |
| 11 | `src/data_dog/agent/loop.py:66,136-156` | `AgentLoop` 最多 8 步；`LLMCallError` 在**这一层**不重试——重试已下移到 provider 包装器（第 24 行），避免两层各重试一遍把预算乘起来 | ✅ 8 步 · — 不重试 · — | 合规（重试职责单一化） |
| 12 | `src/data_dog/mcp_context.py:46-78,190-215,258-320` | 看门狗由 `timeout_s` **派生**（`timeout_s + 0.5s`，保证 ≥ 调用超时）；两次尝试之间**抖动退避**；整次调用受总预算 30s 约束，每次尝试超时被夹到剩余预算内 | ✅ 2 次 · ✅ 30s 总预算 · ✅ 抖动退避 | **合规**（已加固，§3.3-E） |
| 13 | `src/data_dog/knowledge.py:23,61,121-131` | chroma-mcp：单次请求看门狗 60s；进程死则重建一次；收尾 `proc.wait(timeout=1)` | ✅ 1 次 · ✅ 60s · — | 合规 |
| 14 | `src/data_dog/operator_factory/sandbox.py:115,134,144-160` | 子进程 IPC 等待：ready 30s、求值 `wall_s+0.5`、`join(2.0)`；超时**杀进程并抛** `SandboxViolation` | ✅（杀+抛） · ✅ 30s / wall_s+0.5 / 2s · — | 合规 |
| 15 | `src/data_dog/operator_factory/dsl.py:353-378` | 解释器 Tier 0 预算：`max_steps`（默认 10 万）+ 墙钟 `wall_s`（沙箱默认 50ms），节点级检查后抛违规 | ✅ 步数 · ✅ 墙钟 · — | 合规 |
| 16 | `src/data_dog/stream/base.py:94-102` | `poll(timeout)` 用条件变量 `wait(deadline - now)`，单次等待有上限 | — 单次 · ✅ timeout · — | 合规 |
| 17 | `src/data_dog/stream/runner.py:36-88,303-365` | 源轮询异常 → 次数 30 / 墙钟 120s（先到为准）+ 抖动指数退避（0.05s→2s）；耗尽**停止消费**并把 `stop_reason=source_unavailable`、`poll_failures_exhausted` 写进 `stats`（`/healthz` 可见） | ✅ 30 次 · ✅ 120s · ✅ 指数+抖动 | **合规**（已加固，§3.3-B） |
| 18 | `src/data_dog/service/investigation_jobs.py:128-133,148-168` | worker 常驻循环：取不到活就 `Event.wait(poll_s=0.2)`；`stop(wait=False)` 不等线程退出 | — 单次等待有上限 · ✅ 0.2s · — | 无需（附带问题见 §3.2） |
| 19 | `src/data_dog/state/checkpoint.py:129-131`、`src/data_dog/alerting/escalation.py:277` | 周期任务 `Event.wait(interval)`（`while not self._stop.wait(...)`）：停止信号立即唤醒，不拖满间隔 | — 单次等待有上限 · ✅ interval · — | 无需 |
| 20 | `src/data_dog/stream/bus.py:140-142`、`src/data_dog/stream/file.py:99` | 源 `poll(timeout)` 透传为 kafka `timeout_ms` / 文件轮询；单次有上限 | — 单次 · ✅ timeout · — | 无需 |
| 21 | `src/data_dog/state/lease.py`（`Lease.acquire/renew`） | 单持有者租约 TTL（file 源默认 30s、升级 sweeper 默认 60s）+ 每轮 `renew()`；接管判据是**过期**（时间上限），无重试环。**已移到 `state/`**：它是 `StateStore` 之上的通用原语，file 源与告警 sweeper 共用 | ✅（TTL 过期即抢） · ✅ TTL · — | 合规 |
| 22 | `src/data_dog/paths.py:27`、`src/data_dog/operator_factory/verification.py:61`、`src/data_dog/mujoco_sim/simulate.py:211`、`src/data_dog/operator_factory/dsl.py:182,246,555,562` | `for _ in range(...)` / `while True` 是**目录上溯 / 数值迭代 / 采样 / DSL 解析循环**，不是重试 | — | 无需 |
| 23 | `src/data_dog/cli.py:151`、`src/data_dog/operator_factory/sandbox_exec.py:143`、`src/data_dog/knowledge.py:105`、`src/data_dog/mcp_context.py:176`、`src/data_dog/state/redis.py:158` | `while True` 是**交互 REPL / worker 主循环 / stdout 读循环 / SCAN 游标**，不是重试 | — | 无需 |
| 24 | `src/data_dog/service/runtime.py`（`_RetryProvider`，装配在 `_DeadlineProvider` 内） | 单次 `chat()` 的**有界重试**：次数 = `1 + DD_LLM_RETRIES`（默认 1）；只重试 `LLMCallError.retryable`（超时/不可用/被限流），**鉴权与入参不重试**；退避 `base*2^n × U(0.5,1.5)`；总预算耗尽（`retryable=False`）**一次都不重试**；重试耗尽后可用 `DD_LLM_FALLBACK_MODEL` 再试一次 | ✅（默认 2 次尝试） · ✅ 受外层 90s 夹住 · ✅ 指数+抖动 | **合规**（新增，§3.3-D 的补全） |

---

## 3. 清单：待改 / 附带问题 / 已处理

### 3.1 需要改实现（按严重度）

**已清空**：初版审计列出的 7 项待改（A~G）已全部落地——A/F/B/C/D/E/G 见 §3.3。
两条低优先级项（I=CMDB 拉取固定 5s 无重试、J=`stop(wait=False)`）按"写明理由与触发条件"
处理，见 §3.2，而不是当成漏审。

### 3.2 未修（低）：为什么不修 + 何时该改

这两条都**不满足"≤5 行且零风险"**的顺手修标准，故按"写明理由与触发条件"处理。

| # | 位置 | 为什么不修 | 何时该改（触发条件） |
|---|---|---|---|
| I | `src/data_dog/evidence/change_source.py:229-231` | 单次 `urlopen(timeout=5s)`、无重试，是**有意的**：① 失败是**显式**的（异常上抛，绝不回空）；② 它在 Agent 工具调用链里同步执行，加重试=把 CMDB 的抖动变成调查耗时放大；③ `WebhookChangeSource` 是**拉取式只读查询**，重投没有副作用收益（不像投递需要"至少一次"）。唯一真缺口是"被拒"与"无数据"要靠 `last_reason` 区分——那是**可观测性**问题，不是重试问题。 | 若生产观测到 CMDB 的 p99 > 5s 或瞬时抖动率 > 1%（导致调查频繁缺少变更证据），或该来源改为**推送式**（`DbChangeSource`）后仍需拉取兜底 → 此时加"2 次 + 1s 抖动退避"的显式预算，并把 `last_reason` 一并回给 Agent。 |
| J | `src/data_dog/service/investigation_jobs.py:128-133` | `stop(wait=False)` 是**关停语义的选择**而非疏漏：worker 可能正在跑 LLM 调查（单次数十秒），HTTP/CLI 关停若等它，就是把"停服"绑在最长那次外部调用上；而 `ThreadPoolExecutor.shutdown(wait=False)` 只是不 join，**不会**丢已提交的 job（job 行留在 `pending`，下次启动可续）。已知代价是测试收尾的空窗竞态，`tests/conftest.py` 已用"排干 `dd-investigate` 线程"兜住。 | 若引入**优雅关停/滚动发布**（需要"收到 SIGTERM 后在 N 秒内跑完在途调查"），或生产出现"重启后 job 卡在 pending 且无人认领" → 此时加**有界排空**（`drain_timeout_s`，默认 0=保持现状），并在 `/readyz` 暴露"在途调查数"。 |

### 3.3 本轮已处理（加固记录）

| # | 位置 | 最终口径 | 验证 |
|---|---|---|---|
| A（高） | `src/data_dog/storage/db.py:85-149`、`src/data_dog/state/pg.py:29-44`、`src/data_dog/settings.py:112-133` | 新增**唯一实现** `engine_kwargs(url)`：PG 分支设 `connect_timeout`（默认 5s）、`pool_timeout`（默认 5s）、`statement_timeout`（默认 15s，走 libpq `options`，0=不限）并保留 `pool_pre_ping`；URL 里已有的 `?options=...`（如 `-csearch_path=...`）**合并而非覆盖**；SQLite 分支返回既有 `check_same_thread=False`，**行为逐字节不变**。业务库与 `PostgresStateStore` 共用同一份参数（不许两处各写一套而漂移）。 | `tests/test_reliability_hardening.py` 8 条（含用 `do_connect` 事件截获"最终交给 libpq 的参数"、URL options 合并、SQLite kwargs 不变、SQLite 池超时仍为 SQLAlchemy 默认 30s） |
| F（中） | `src/data_dog/service/evidence.py:260-295`、`src/data_dog/settings.py:450` | 变更源抛错 → **失败关闭**：返回 `err(UNAVAILABLE, reason="change_source_unavailable")`，**不含 `changes` 字段**（工具层对 `ok=False` 原样透传，Agent 无从据此下"近期无变化"的结论）；样例回退改为显式开关 `DD_CHANGE_FALLBACK_DEMO`（默认关），打开时结果带 `degraded=True`。 | 同文件 6 条（错误形状、工具层透传、默认关、demo 开、正常路径与 denied 语义不变） |
| H（低） | `src/data_dog/state/redis.py:45-73` | 已改（他人）：冲突判定改为**惰性导入 `redis.exceptions.WatchError` + `isinstance`**；仅当未安装 redis（拿不到类）时才退回类名比对，并在 docstring 里写明兜底分支的已知失效条件。 | 已读源码确认（`_watch_error_class` / `_is_watch_error`） |
| B（中） | `src/data_dog/stream/runner.py:36-88,303-365` | 新增 `PollRetryPolicy`：轮询失败按**次数 30 / 墙钟 120s（先到为准）** + 抖动指数退避（0.05s→2s × `U(0.5,1.5)`）。耗尽时**停止消费**并把 `stop_reason="source_unavailable"` 与 `poll_failures_exhausted` 写进 `stats`（`/healthz` 直接回吐 stats，故不改任何路由即可见），同时 `_log.error` 打一条含次数/耗时的错。为什么"停+置状态"而不是抛异常：`run()` 的调用方都在长驻守护线程里，抛异常只会带 traceback 静默死掉并**跳过收尾 `flush()`**（残留候选不落库）；返回则调用方零改动、状态可观测。 | `test_stream_poll_failures_exhaust_count_budget_and_expose_reason`、`test_stream_poll_failures_exhaust_wall_clock_budget_first`、`test_stream_poll_backoff_is_jittered_and_bounded`、`test_stream_runner_recovers_after_transient_poll_error` |
| C（中） | `src/data_dog/alerting/outbox.py:42-48,109-215` | `deliver()` 改为**单次调用内**按"次数 3 / 墙钟 2s（先到为准）"重试，间隔是抖动指数退避（0.05s→0.5s × `U(0.5,1.5)`，`sleep`/`rand` 可注入）；耗尽进死信并写 `dead_reason`（`max_attempts` / `retry_deadline`）、`stats["deadline_exceeded"]`、`stats["retries"]` + 一条 warning。**至少一次 + 一次外部效果不变**：成功或死信前一直是 `pending`，且每次重试复用同一 `(幂等键, revision)`（去重仍由该二元组保证）。 | `test_outbox_wall_clock_budget_marks_dead_below_max_attempts`、`test_outbox_attempts_budget_marks_dead_with_reason`、`test_outbox_deliver_retries_in_one_call_and_keeps_idempotency`、`test_outbox_backoff_is_jittered_and_bounded` |
| D（中） | `src/data_dog/providers/client.py:1-13,98-121` | `OpenAI(..., max_retries=0)`：一次 `chat()` = **一次** HTTP 往返，超时仍是我们显式传的 `DD_LLM_TIMEOUT_S`（默认 60s）。为什么不让 SDK 自己重试：SDK 默认 2 次重试**没有墙钟上限**且重试的是整次往返，最坏 ≈3×60s；而 `_DeadlineProvider` 的 90s 预算只在**每次调用前**检查，拦不住进行中的重试——曾出现"预算说 90s、实际能跑 3 分钟"。重试预算归上层（知道剩余预算的一方）。 | `test_provider_disables_sdk_level_retries`、`test_provider_chat_passes_our_timeout_and_does_not_retry` |
| E（中） | `src/data_dog/mcp_context.py:44-78,190-215,258-320` | 三处：① 看门狗改为 `_watchdog_s(timeout_s) = timeout_s + 0.5s`，**保证"看门狗 ≥ 本次调用超时"**（此前等于超时，等于在超时点上把还可能活着的 server 杀掉）；② 握手 `_start(timeout_s)` 也接收超时，不再固定 20s（否则"把超时调小"对手握无效）；③ `fetch_trusted_context` 总预算 30s，每次尝试超时 = `min(timeout_s, 剩余预算)`，两次尝试之间**抖动退避**（0.25s × `U(0.5,1.5)`），预算不足即不再发起；全部失败 → `McpContextError`（含尝试次数与原错误），并把常驻单例置空。 | `test_mcp_watchdog_is_never_shorter_than_call_timeout`、`test_mcp_attempt_timeout_is_clamped_by_remaining_budget`、`test_mcp_retry_backoff_is_jittered_and_bounded`、`test_mcp_total_budget_gate_blocks_attempts`、`test_mcp_normal_path_still_fetches_context` |

| G（低） | `src/data_dog/settings.py:653-661`、`src/data_dog/alerting/notifiers.py:213-214` | 新增**独立**配置项 `DD_SMTP_TIMEOUT_S`（默认 5s，与旧值一致）并让邮件通道用它。此前 SMTP 直接复用 `DD_NOTIFY_HTTP_TIMEOUT_S`，于是"把邮件超时调长"会连带放长 webhook 超时（反之亦然）——两类下游特性完全不同（SMTP 要 TLS 握手 + 认证 + 中继排队，webhook 是一次 HTTP POST）。属**解耦**而非改行为。 | `tests/test_notifiers_paths.py::test_smtp_timeout_is_decoupled_from_http_timeout`（两项独立取值 + SMTP 实例实际收到 17s） |

**边界（A / B / C / D / E / F / G 的诚实声明）**：

- 超时值是**设计默认**而非压测结论：5s 与仓内其它"快速失败"预算同量级（redis 冲突重试 5s、
  通知 HTTP 5s），15s 的 `statement_timeout` 远高于应用查询量级（`docs/slo.md` 热路径为毫秒级）。
  真实生产负载下应据 p99 校准，全部可用 `DD_DB_*` 调整，无需改代码。
- `statement_timeout` **不影响迁移**：alembic 走自己的引擎（`storage/migrations.py`），
  长迁移不会被误杀。
- F 的失败关闭只覆盖**异常路径**；"来源返回空列表"仍是**来源的合法答案**（例如 jsonl 里
  确实没有该部件的记录），此时 `source` 会如实标注为具体来源名——这是"查得到、确实没有"，
  与"查不到"必须区分开。
- F 的失败关闭覆盖**异常路径**；`tests/test_change_source.py` 已随之更新为两条用例：
  `test_bridge_fails_closed_on_source_error`（默认失败关闭、响应体**不含** `changes` 字段）
  与 `test_bridge_demo_fallback_requires_explicit_switch`（只有显式 `DD_CHANGE_FALLBACK_DEMO=1`
  才回退样例且带 `degraded=True`）。
- B 的"停止消费"是**终态**：`run()` 返回后不会自愈，需要重启（或由运维重新拉起）。
  这是刻意的——源地址写错这类问题重启也会立刻复现，而"继续跑但永不前进"才是最难排查的形态。
  代价：真正长达数分钟的 broker 故障（超过 30 次/120s）会导致该副本停止消费，需靠
  `/healthz` 的 `stop_reason` 触发告警后人工/编排层重启。若生产出现"故障时长常态超过 2 分钟"，
  调大 `PollRetryPolicy(budget_s=…)` 即可（构造时注入，`StreamRunner(poll_retry=…)`）。
- B/C 的**退避会真的睡**（不是零成本记账）：C 的最坏阻塞 = `retry_deadline_s`（默认 2s）+ 一次
  适配器调用耗时，且它跑在调用方线程里（P3 上报路径）。这正是"把重试预算收回自己手里"的
  代价——换来的是"不会毫秒级连打下游"和"有确定上界"。
- D 的残余风险：若某个供应商的 SDK 在**传输层**（如 httpcore）自带重连/重试，`max_retries=0`
  管不到那一层；本仓用 OpenAI 兼容 SDK，实测 `DEFAULT_MAX_RETRIES=2` 已关闭（openai 3.8.0）。
- E 的看门狗仍是**唯一的硬上界**（`_recv` 的 readline 没有超时参数）：`timeout_s` 只决定
  "多晚杀"，一旦看门狗因极端调度延迟没触发，本次调用仍会阻塞。真正的兜底是
  `_RETRY_TOTAL_BUDGET_S` 与上层调查预算。

---

## 4. 为什么这些点判"无需改造"

- **第 2、22、23 行（各类 `while True` / `for _ in range`）**：它们是解析器循环、SCAN 游标、
  数值迭代、采样计数、REPL 与 worker 主循环——**没有"重试同一操作"的语义**，因此
  "次数+时间双上限+抖动退避"不适用；它们的阻塞点（若在等待）已单独成行审计。
- **第 6、8 行（对外 HTTP 的一次性调用）**：设计上就是"单次尝试 + 单次超时 + 失败如实上报"，
  重投由上层（升级引擎 / 下一轮拉取）负责，退避不适用。
- **第 18、19、20 行（等待/轮询）**：等待本身带 timeout，且由停止信号或上游 poll 保证可退出，
  属于"有界等待"而非重试。
- **第 10、11 行（调查预算与 Agent 步数）**：整次调查由 `max_steps=8` + 90s 双上限封住，
  LLM 失败**有意不重试**（失败关闭优于反复试错）——这是规范之外的合理豁免，故记"合规"。

---

## 5. 审计方法与边界

**方法（可复现，全部只读）**：

```
ruff/mypy 之外，用以下检索定位候选点（`src/` 下）：
  for _ in range( | while True | tenacity | backoff | jitter | deadline | time.sleep
  timeout= | REQUEST_TIMEOUT | pool_timeout | connect_timeout | Timer(
  .wait( | .get(timeout | join(timeout | poll( | max_attempts | attempts >=
  httpx | requests. | urlopen | socket.setdefaulttimeout | openai
```

逐个点**读源码上下文**后再判定（不靠命名猜语义）：`state/redis.py`、`state/pg.py`、
`storage/db.py`、`alerting/outbox.py`、`alerting/notifiers.py`、`evidence/change_source.py`、
`providers/client.py`、`service/app.py`、`service/investigation_jobs.py`、`agent/loop.py`、
`mcp_context.py`、`knowledge.py`、`operator_factory/{sandbox,dsl,sandbox_exec,verification}.py`、
`stream/{base,runner,bus,file,lease}.py`、`state/checkpoint.py`、`alerting/escalation.py`。

**边界与未覆盖项**（诚实声明）：

1. **只审计静态代码**：表中"最坏阻塞"是按代码常量与库默认值推导的**上界估计**，
   不是实测值；生产参数（`DD_*` 环境变量）可能与默认值不同。
2. **HTTP 层路由的故障注入测试不在本文范围**（由 service 拆分后的 HTTP 层测试补齐）；
   本文只审计了 `service/app.py` 里与超时预算相关的代码。
3. **第三方库内部行为按文档默认值记录**（尤其 OpenAI SDK 的 `max_retries` 默认 2、
   libpq 的 `connect_timeout` 默认无限）；版本升级后需重新核对。本次实测核对：
   `openai 3.8.0` 的 `DEFAULT_MAX_RETRIES == 2`（`OpenAI.__init__` 的 `timeout` 默认 `NOT_GIVEN`，
   故超时**完全**依赖调用点显式传 `timeout`，见第 9 行）；该默认重试已由 D（§3.3）显式关闭。
4. **异步/后台任务的"最终是否收敛"未做压测**：例如升级 sweeper、checkpoint loop 的长跑
   稳定性，需要更长时长的故障注入才能验证。
5. **快照与加固的关系**：§2 是审计快照，已按两轮加固结果就地更新（结论列标"已加固"）；
   §3.3 记录改动内容、最终口径与边界。为不与主代理正在进行的 `service/app.py` / `deps.py` /
   `routes/**` 拆分冲突，两轮合计只动了：`storage/db.py`、`state/pg.py`、`settings.py`、
   `service/evidence.py`（第一轮）与 `stream/runner.py`、`alerting/outbox.py`、
   `providers/client.py`、`mcp_context.py`（第二轮），并新增一个测试文件；
   其余审计发现一律只记录、不顺手改（§3.1 / §3.2）。

---

## 6. 关联

- 规范样板的实现与理由：`src/data_dog/state/redis.py`（模块 docstring + `_RETRY_*` 注释）。
- 失败关闭的行为断言：`tests/test_failure_injection.py`（22 条非 HTTP 层故障注入用例：
  并发不丢更新、限流不超发、outbox 不重复、变更源/水位线/MCP 失败关闭）。
- **跨进程一致性**（不是"单进程里假装多副本"）：`tests/test_state_multiprocess.py` 用
  `multiprocessing` 真开进程打同一个 PG，盯三件事：租约赢家唯一、限流配额共享（4 副本 × 5 次
  只放行 burst 次，而非每副本一份）、`mutate` 跨进程不丢更新。文件头带**变异矩阵**：
  去掉 `state/pg.py` 的 `FOR UPDATE` → 限流超发（12 次）且计数器 33≠100；把桶 key 按 pid 分片
  → 放行 20 次。也如实写明**租约那条对原子性变异不敏感**（靠计数器那条兜）。
- 跨副本限流的时钟边界：令牌桶的 `last` 是跨副本共享值，故必须用**墙钟**（与租约同一条
  边界，生产须跑 NTP）。曾经它用的是 `time.monotonic()`——各进程私有纪元，主机 B 写下的
  空桶会让主机 A 算出负 elapsed，令牌被倒扣到 -4.35e6（按 1 req/s 就是该副本约 50 天全量
  429）。这里有**两层**，别只修一层：
  1. `StateStore.rate_limit_allow` 的**默认时钟**（调用方不传 `now=` 时）；
  2. `data_plane/gateway.py` 的 `self._clock`——它是限流的**唯一生产入口**，曾经显式注入
     `time.monotonic`，于是只修默认值等于没修（修完仍会“只扣不补、永久 429”）。
  回归断言：`tests/test_state_store.py::test_rate_limit_default_clock_is_wall_clock_not_monotonic`、
  `::test_rate_limit_backward_clock_does_not_lock_out`（存储层），以及
  `::test_gateway_rate_limit_uses_wall_clock`（**走生产接线**：构造真 `TelemetryGateway`
  调 `_allow`，断言桶内时间戳是墙钟量级）；三条在旧实现下均确定性变红。
  同一条边界也决定了 `StreamMessage.enqueued_at` 的适用前提：它用 monotonic 打点，
  所以**打点与消费必须在同一进程**；将来若让源在另一个进程打点，消费延迟会算错。
- 本轮加固的回归断言：`tests/test_reliability_hardening.py`（29 条：PG 三段超时 + SQLite 行为
  不变 + 变更源失败关闭/demo 开关 + 流式源轮询预算 + outbox 投递预算 + LLM `max_retries=0` +
  MCP 看门狗/退避/总预算，均含"预算耗尽 → 显式失败/可观测"与"正常路径不变"两侧）。
- 时延预算：`docs/slo.md`（本表第 15 行的 50ms 沙箱墙钟对齐其 §4；§3.3 选 15s
  `statement_timeout` 的依据同样来自它——热路径预算是毫秒级，15s 只兜"病态慢"）。
