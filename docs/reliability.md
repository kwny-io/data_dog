# data_dog 重试与超时审计（可靠性 v0.1）

> **交付物**：`src/**` 全量重试/超时点审计表 + 待改清单。
> **范围声明**：本文**只审计、不改任何实现代码**。审计对象是 `src` 下的重试环路、超时参数、
> 阻塞等待与退避策略；HTTP 层（`service/app.py` 路由）的**故障注入测试**不在本文范围（由
> service 拆分后的 HTTP 层测试补齐），但该文件里的超时预算已纳入审计。
> **基线**：`src/data_dog/state/redis.py` 的 `mutate()` 是本仓当前的规范样板（§1）。

---

## 0. TL;DR

- 审计 **23 个点**：**合规 12**、**无需改造 6**、**待改 5**（4 中 + 1 低）。
  「无需」的 6 条是明确豁免（见 §4），不是漏审。
- **已处理 3 项**（§3.3）：A=PG 三段超时（高）、F=变更源失败关闭（中），
  以及外部已修的 H（redis `WatchError` 按类型判定；G 的 SMTP 配置项**未**改，仍在 §3.1）。
- **唯一的规范样板**是 `state/redis.py:58` 的 `mutate()`：次数上限 512 + 墙钟上限 5s +
  指数退避 × 抖动。其余重试点**没有第二个**做到三件套齐全。
- 最该修的下一个：**流式源轮询失败无限重试且固定退避**（`stream/runner.py:252`，中）；
  其余待改项见 §3.1。
- 大量 `while True` / `for _ in range(...)` **不是重试**（解析器、采样、交互、worker 主循环），
  已逐条排除，见 §4。

---

## 1. 规范：什么叫"次数 + 时间双上限 + 抖动退避"

样板：`src/data_dog/state/redis.py:58-86`（常量在 `:37-40`）

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
| 1 | `src/data_dog/state/redis.py:58-86`（常量 `:37-40`） | `mutate` 冲突重试：512 次 / 5s 双上限，指数退避（0.5ms→3ms）× `U(0.5,1.5)` 抖动，耗尽抛 `RuntimeError` | ✅ 512 次 · ✅ 5s · ✅ 指数+抖动 | **合规（样板）** |
| 2 | `src/data_dog/state/redis.py:91-102` | `keys()` 用 SCAN 游标循环分页（`count=500`）；不是重试环 | — | 无需 |
| 3 | `src/data_dog/state/pg.py:34-46` | `mutate` 用 `SELECT ... FOR UPDATE` 行锁串行化（无重试环）；引擎参数复用 `storage.db.engine_kwargs`，锁等待/建连均有上限 | — 无重试 · ✅ 复用的三段超时 · — | **合规**（已加固，§3.3-A） |
| 4 | `src/data_dog/storage/db.py:85-130`（配置项 `settings.py:112-133`） | `engine_kwargs`：PG 设 `connect_timeout=5s` / `pool_timeout=5s` / `statement_timeout=15s` + `pool_pre_ping`；SQLite 分支**不变** | — 无重试 · ✅ 5s / 5s / 15s（`DD_DB_*` 可调，0=不限语句） · — | **合规**（已加固，§3.3-A） |
| 5 | `src/data_dog/alerting/outbox.py:81,101-122` | `deliver()`：仅次数上限 `max_attempts=3`；**同一次调用内不重试**，两次尝试的间隔完全由调用方调度决定；无墙钟上限、无退避抖动 | ✅ 3 次 · ❌ 无时间上限 · ❌ 无退避 | **待改** |
| 6 | `src/data_dog/alerting/notifiers.py:134-149`（`_post_json`，调用点 `:165,:189`） | HTTP/IM 通知：单次 `urlopen(timeout=DD_NOTIFY_HTTP_TIMEOUT_S)`（默认 5s），不做重试，失败如实返回 `DeliveryResult(ok=False, detail=…)` | — 单次 · ✅ 5s · — | 合规 |
| 7 | `src/data_dog/alerting/notifiers.py:212-222` | 邮件通道 `smtplib.SMTP(..., timeout=DD_NOTIFY_HTTP_TIMEOUT_S)`；无重试 | — 单次 · ✅ 5s（复用 HTTP 配置项） · — | **待改（低）** |
| 8 | `src/data_dog/evidence/change_source.py:229-231,250-260` | CMDB 拉取：单次 `urlopen(timeout=self.timeout)`（默认 5s，可注入），**异常直接上抛**、不回空 | — 单次 · ✅ 5s · — | 合规 |
| 9 | `src/data_dog/providers/client.py:101-102,135-148`（默认值 `:44-49`） | LLM 调用：显式 `timeout=DD_LLM_TIMEOUT_S`（默认 60s）；**未传 `max_retries`** → OpenAI SDK 默认隐式重试 2 次 | ⚠️ 3 次（SDK 隐式）· ❌ 无墙钟上限 · ✅ SDK 指数+抖动 | **待改** |
| 10 | `src/data_dog/service/app.py:1927-1939`（装配 `:1949-1968`） | `_DeadlineProvider` 给整次调查加 `DD_INVESTIGATE_TIMEOUT_S`（默认 90s）总预算，到点抛 `LLMCallError(TIMEOUT)` → Agent 失败关闭 | ✅（步数 `max_steps=8`）· ✅ 90s · — | 合规 |
| 11 | `src/data_dog/agent/loop.py:66,136-156` | `AgentLoop` 最多 8 步；`LLMCallError`（超时/不可用）**立即失败关闭，不重试** | ✅ 8 步 · — 不重试 · — | 合规（有意不重试） |
| 12 | `src/data_dog/mcp_context.py:27,104-114,143-157,203-228` | 每次 JSON-RPC 请求挂看门狗 `REQUEST_TIMEOUT_S=20s`（超时杀进程）；`fetch_trusted_context` 失败后"关旧建新"**重试一次**（立即重试）；无总时间预算 | ✅ 2 次 · ❌ 无总上限（最坏 2×20s + 2×timeout_s） · ❌ 无退避（立即重试） | **待改** |
| 13 | `src/data_dog/knowledge.py:23,61,121-131` | chroma-mcp：单次请求看门狗 60s；进程死则重建一次；收尾 `proc.wait(timeout=1)` | ✅ 1 次 · ✅ 60s · — | 合规 |
| 14 | `src/data_dog/operator_factory/sandbox.py:115,134,144-160` | 子进程 IPC 等待：ready 30s、求值 `wall_s+0.5`、`join(2.0)`；超时**杀进程并抛** `SandboxViolation` | ✅（杀+抛） · ✅ 30s / wall_s+0.5 / 2s · — | 合规 |
| 15 | `src/data_dog/operator_factory/dsl.py:353-378` | 解释器 Tier 0 预算：`max_steps`（默认 10 万）+ 墙钟 `wall_s`（沙箱默认 50ms），节点级检查后抛违规 | ✅ 步数 · ✅ 墙钟 · — | 合规 |
| 16 | `src/data_dog/stream/base.py:94-102` | `poll(timeout)` 用条件变量 `wait(deadline - now)`，单次等待有上限 | — 单次 · ✅ timeout · — | 合规 |
| 17 | `src/data_dog/stream/runner.py:252-261` | 源轮询异常 → **无限重试**，固定 `sleep(min(1.0, poll_timeout))`，只计数 `poll_errors` 后继续 | ❌ 无次数上限 · ❌ 无总时间上限 · ❌ 固定退避（无抖动） | **待改** |
| 18 | `src/data_dog/service/investigation_jobs.py:128-133,148-168` | worker 常驻循环：取不到活就 `Event.wait(poll_s=0.2)`；`stop(wait=False)` 不等线程退出 | — 单次等待有上限 · ✅ 0.2s · — | 无需（附带问题见 §3.2） |
| 19 | `src/data_dog/state/checkpoint.py:129-131`、`src/data_dog/alerting/escalation.py:277` | 周期任务 `Event.wait(interval)`（`while not self._stop.wait(...)`）：停止信号立即唤醒，不拖满间隔 | — 单次等待有上限 · ✅ interval · — | 无需 |
| 20 | `src/data_dog/stream/bus.py:140-142`、`src/data_dog/stream/file.py:99` | 源 `poll(timeout)` 透传为 kafka `timeout_ms` / 文件轮询；单次有上限 | — 单次 · ✅ timeout · — | 无需 |
| 21 | `src/data_dog/stream/lease.py:45-94` | file 消费者租约 TTL（默认 30s）+ 每轮 `renew()`；接管判据是**过期**（时间上限），无重试环 | ✅（TTL 过期即抢） · ✅ TTL · — | 合规 |
| 22 | `src/data_dog/paths.py:27`、`src/data_dog/operator_factory/verification.py:61`、`src/data_dog/mujoco_sim/simulate.py:211`、`src/data_dog/operator_factory/dsl.py:182,246,555,562` | `for _ in range(...)` / `while True` 是**目录上溯 / 数值迭代 / 采样 / DSL 解析循环**，不是重试 | — | 无需 |
| 23 | `src/data_dog/cli.py:151`、`src/data_dog/operator_factory/sandbox_exec.py:143`、`src/data_dog/knowledge.py:105`、`src/data_dog/mcp_context.py:124`、`src/data_dog/state/redis.py:96` | `while True` 是**交互 REPL / worker 主循环 / stdout 读循环 / SCAN 游标**，不是重试 | — | 无需 |

---

## 3. 清单：待改 / 附带问题 / 已处理

### 3.1 需要改实现（按严重度）

| # | 位置 | 问题（一句话） | 严重度 |
|---|---|---|---|
| B | `src/data_dog/stream/runner.py:252-261` | 源轮询失败后无限重试，固定 1s 退避且无抖动、无次数/总时间上限——持续故障下永久刷日志，恢复瞬间所有 runner 同步重试（惊群），且没有"放弃并暴露"的终点。 | 中 |
| C | `src/data_dog/alerting/outbox.py:101-122` | 投递重试只有次数上限（3 次），没有墙钟上限与退避抖动，且同一 `deliver()` 内不重试——两次尝试的间隔由调用方决定，可能退化成毫秒级连打同一下游。 | 中 |
| D | `src/data_dog/providers/client.py:101-102` | `OpenAI(...)` 未显式设置 `max_retries`，SDK 默认隐式重试 2 次（有指数退避+抖动、**无墙钟上限**），合起来最坏 ≈3×60s；`_DeadlineProvider` 只在**调用前**检查预算，无法中断进行中的重试——重试行为既不可见也不可配。 | 中 |
| E | `src/data_dog/mcp_context.py:203-228` | 失败后"关旧建新"重试一次且**不等待**（立即重试）；`_start()` 的握手看门狗固定 20s（不随 `timeout_s` 收窄），最坏 2×20s + 2×timeout_s 且无总预算。 | 中 |
| G | `src/data_dog/alerting/notifiers.py:213-214` | SMTP 超时复用 `DD_NOTIFY_HTTP_TIMEOUT_S`（HTTP 语义的配置项），邮件通道无法单独调超时。 | 低 |

### 3.2 附带问题（低）

> 这两条是审计中读到、但未单独成表的实现细节；A / F / H 已处理，见 §3.3。

| # | 位置 | 问题 | 严重度 |
|---|---|---|---|
| I | `src/data_dog/evidence/change_source.py:229-231` | webhook 拉取固定 5s、无重试：CMDB 一次抖动即整体失败（失败关闭本身正确，但可用性偏脆），且调用方需要靠 `last_reason` 才能区分"被拒"与"无数据"。 | 低 |
| J | `src/data_dog/service/investigation_jobs.py:128-133` | `stop(wait=False)` 不等 worker 退出，关停/测试收尾存在空窗竞态（`tests/conftest.py:9-17` 已用"排干线程"兜住）——不是重试/超时问题，但同属"进程边界上缺时间上限"。 | 低 |

### 3.3 本轮已处理（加固记录）

| # | 位置 | 最终口径 | 验证 |
|---|---|---|---|
| A（高） | `src/data_dog/storage/db.py:85-149`、`src/data_dog/state/pg.py:29-44`、`src/data_dog/settings.py:112-133` | 新增**唯一实现** `engine_kwargs(url)`：PG 分支设 `connect_timeout`（默认 5s）、`pool_timeout`（默认 5s）、`statement_timeout`（默认 15s，走 libpq `options`，0=不限）并保留 `pool_pre_ping`；URL 里已有的 `?options=...`（如 `-csearch_path=...`）**合并而非覆盖**；SQLite 分支返回既有 `check_same_thread=False`，**行为逐字节不变**。业务库与 `PostgresStateStore` 共用同一份参数（不许两处各写一套而漂移）。 | `tests/test_reliability_hardening.py` 8 条（含用 `do_connect` 事件截获"最终交给 libpq 的参数"、URL options 合并、SQLite kwargs 不变、SQLite 池超时仍为 SQLAlchemy 默认 30s） |
| F（中） | `src/data_dog/service/evidence.py:260-295`、`src/data_dog/settings.py:450` | 变更源抛错 → **失败关闭**：返回 `err(UNAVAILABLE, reason="change_source_unavailable")`，**不含 `changes` 字段**（工具层对 `ok=False` 原样透传，Agent 无从据此下"近期无变化"的结论）；样例回退改为显式开关 `DD_CHANGE_FALLBACK_DEMO`（默认关），打开时结果带 `degraded=True`。 | 同文件 6 条（错误形状、工具层透传、默认关、demo 开、正常路径与 denied 语义不变） |
| H（低） | `src/data_dog/state/redis.py:45-73` | 已改（他人）：冲突判定改为**惰性导入 `redis.exceptions.WatchError` + `isinstance`**；仅当未安装 redis（拿不到类）时才退回类名比对，并在 docstring 里写明兜底分支的已知失效条件。 | 已读源码确认（`_watch_error_class` / `_is_watch_error`） |

**边界（A / F 的诚实声明）**：

- 超时值是**设计默认**而非压测结论：5s 与仓内其它"快速失败"预算同量级（redis 冲突重试 5s、
  通知 HTTP 5s），15s 的 `statement_timeout` 远高于应用查询量级（`docs/slo.md` 热路径为毫秒级）。
  真实生产负载下应据 p99 校准，全部可用 `DD_DB_*` 调整，无需改代码。
- `statement_timeout` **不影响迁移**：alembic 走自己的引擎（`storage/migrations.py`），
  长迁移不会被误杀。
- F 的失败关闭只覆盖**异常路径**；"来源返回空列表"仍是**来源的合法答案**（例如 jsonl 里
  确实没有该部件的记录），此时 `source` 会如实标注为具体来源名——这是"查得到、确实没有"，
  与"查不到"必须区分开。
- F 只改了**证据桥**这一处；`tests/test_change_source.py::test_bridge_falls_back_on_source_error`
  仍在断言旧的隐式回退口径（该文件不在本次可改范围内），需由主代理按新口径更新为
  "默认失败关闭 / 开 demo 开关才回退"。

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
   故超时**完全**依赖调用点显式传 `timeout`，见第 9 行）。
4. **异步/后台任务的"最终是否收敛"未做压测**：例如升级 sweeper、checkpoint loop 的长跑
   稳定性，需要更长时长的故障注入才能验证。
5. **快照与加固的关系**：§2 是审计快照，其中第 3、4 行已按本轮加固结果就地更新（结论列标
   "已加固"）；§3.3 记录改动内容、最终口径与边界。为不与主代理正在进行的
   `service/app.py` / `deps.py` / `routes/**` 拆分冲突，本轮**只**动了 §3.3 列出的那 4 个文件
   （`storage/db.py`、`state/pg.py`、`service/evidence.py`、`settings.py`）并新增一个测试文件；
   其余审计发现一律只记录、不顺手改。

---

## 6. 关联

- 规范样板的实现与理由：`src/data_dog/state/redis.py`（模块 docstring + `_RETRY_*` 注释）。
- 失败关闭的行为断言：`tests/test_failure_injection.py`（22 条非 HTTP 层故障注入用例：
  并发不丢更新、限流不超发、outbox 不重复、变更源/水位线/MCP 失败关闭）。
- 本轮加固的回归断言：`tests/test_reliability_hardening.py`（14 条：PG 三段超时 +
  SQLite 行为不变 + 变更源失败关闭 / demo 开关 / 正常路径与 denied 语义不变）。
- 时延预算：`docs/slo.md`（本表第 15 行的 50ms 沙箱墙钟对齐其 §4；§3.3 选 15s
  `statement_timeout` 的依据同样来自它——热路径预算是毫秒级，15s 只兜"病态慢"）。
