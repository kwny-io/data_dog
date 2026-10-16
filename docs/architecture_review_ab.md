# 架构评审清单 A/B：落地台账（2026-10-14）

> 来源：以"高级软件架构师"视角对**代码事实**（不是文档）做的评审：A 为真缺口 10 条、
> B 为存量可优化 7 条。本文件是**逐项状态**：做完了什么、证据在哪、**没做的边界与原因**。
> 纪律：每项**独立提交**、带回归测试、文档同步；无法安全完成的项**写明边界**，不含糊过去。

## 总览

| 分组 | 已落地 | 未落地（见下"边界"） |
|---|---|---|
| A（10 条缺口） | A1–A10 **全部落地** | A9 走「摄取日桶」（跨天重投递会多一行边界 + 检测）；A10 的身份体系为边界 |
| B（7 条优化） | B1 B2 B3 B4 B5 B6 B7 | — |

## A. 缺口

| # | 缺口 | 状态 | 提交 | 落地内容（一句话） |
|---|---|---|---|---|
| A1 | LLM 花费/额度无硬约束 | ✅ | `b0142c8` | per-tenant 日/月 token+USD 硬熔断，调用前后各一次检查；超限 429 + `error_code=budget_exceeded` |
| A2 | 队列无准入/背压 | ✅ | `b0142c8` | 共享队列深度 + `DD_INVESTIG@E_QUEUE_MAX`；超限 **落 job 之前** 429 + `Retry-After`（不做假受理） |
| A3 | Prompt 注入只有 mock 回归 | ✅ | `afd62f4` `49bd1d0` | 不可信文本包成数据块 + 系统提示声明指令层级 + 注入特征进 trace；红队入口 `make redteam`。后续修掉数据块导致的**评测静默失效**（`unwrap` 剥壳） |
| A4 | 提交侧无幂等 | ✅ | `b0142c8` | `Idempotency-Key`：窗口内同 (租户,key) 只真跑一次；重放带 `idempotent_replay`；进行中 409 + `Retry-After` |
| A5 | PG 上 JSON 列是 `json` 不是 `jsonb` | ✅ | `51091f1` | 方言变体 + 迁移 `f2a3b4c5d6e7` ALTER 既有库；真 PG 16 验证 24/24 列 `jsonb`、可逆 |
| A6 | 多副本下 retention 无租约 | ✅ | `bf75343` | 真删前抢 `retention:purge` 单持有者租约；抢不到本轮**跳过**并如实标注（≠ 删了 0 行） |
| A7 | 没有告警规则 / SLO 未落地 | ✅ | `574b384` | `PrometheusRule` 12 条规则（可用性/热路径 P99/治理积压/静默失败）+ `docs/slo.md` §9 映射表 + 不依赖 helm 的 meta 测试 |
| A8 | LLM 无降级链 | ✅ | `b0142c8` | 有界重试（只重试可重试错、带抖动、每次重过 deadline）+ 备用模型；`DD_LLM_RETRIES` / `DD_LLM_FALLBACK_MODEL` |
| A9 | `telemetry_events` 没有分区 | ✅ | `46b344c` + 本轮 | 走「摄取日桶」（代价：跨天重投递多一行，有检测函数）：schema/写路径/迁移 → 分区模块日粒度 → 转换脚本；三段均在**真 PG** 验证（含真表转换 + 写路径落分区 + 整段 DROP）；**多副本 DDL 互斥**：`partition:ddl` 单持有者租约（CLI 与启动自动补齐共用，抢不到退出码 3 + `skipped=lease_held`），见下 |
| A10 | 控制台仍单租户 + 两套 UI | ✅ | 见下 | 收敛决定：**产品控制台 = `web/`，`ui/` 只作演示台**（README/交付文档写明）；多租户最小步：`DD_WEB_TENANTS` 服务端**允许清单** + `dd_web_tenant` cookie 做租户**选择**（清单外 403、未配置时行为不变）。**用户级身份/RBAC 仍是人工边界** |

## B. 存量可优化

| # | 优化项 | 状态 | 提交 | 落地内容（一句话） |
|---|---|---|---|---|
| B1 | 同步路 histogram 顶格 2.5s → 全落 +Inf | ✅ | `51091f1` | 桶补到 120s（异步路另有 job 直方图），同步调查 P95/P99 重新可观测 |
| B2 | 同步仍是默认 | ✅ | `d3b5f9d` `38751dc` `473a3d9` | 三步：① 异步结果一次取齐 `report/verdict/trace/usage`（同源 `_verdict_for`）；② 控制台改"提交→轮询→取结果"；③ `DD_INVESTIG@E_ASYNC` **默认 1**（测试用 conftest 钉成同步，默认值另有回归）；见下 |
| B3 | 运行日志写放大（40 步 ≤160 KB/次） | ✅ | `8213327` | `DD_RUN_LOG_FULL_STEPS=auto`：成功且已判定只落**骨架**，失败/取消/`needs_human_review` 落全文；`steps_truncated` 如实标注 |
| B4 | 配置治理：60+ `DD_*` 无启动期校验 | ✅ | `0e785fe` | `data-dog config check` / `make config-check`：未知键、非法取值（会静默回默认）、危险组合；不连库、有错即非零退出 |
| B5 | meta 测试靠人记得跑 | ✅ | `17f775a` | `readme_counts.py --check` 挂进 pre-commit，且**无条件**跑（漂移常发生在没有 .py 变更的提交里） |
| B6 | `run_<uuid16>` 可读性 | ✅ | `9caeda2` | `run_<候选缩写>_<UTC时间戳>_<4位hex>`（4 位随机尾防同一秒重跑撞主键覆盖日志） |
| B7 | 文档断言缺自动核对 | ✅ | `51091f1` | 两条 meta 测试把"声明 == 事实"钉住：逐列按 PG 方言编译检查 + 迁移清单与模型元数据逐条比对 |

## 未落地项：边界与原因

### B2 同步默认翻异步（`DD_INVESTIG@E_ASYNC`）——✅ 三步全部落地（`d3b5f9d` `38751dc` `473a3d9`）

**第 1 步（后端补载荷）已完成**：新增 `GET /v1/investigations/jobs/{id}/result`，成功后就地取齐
`report` + `verdict`/`verdict_detail` + `trace` + `usage`，口径与同步响应**同源**
（`runtime._verdict_for` 一处实现）。

复核时**修正了一处此前的判断错误**：控制台其实**不消费 `alerting`**
（`web/lib/api/types.ts` 的 `InvestigationResult` 里没有这个字段）——它渲染的是
`verdict`/`verdict_detail`/`trace`/`usage`。所以"异步拿不到 alerting"不是阻塞点（而且
`alerting` 是**执行期副作用**的结果，读路径重建会重复上报，本就不该在结果端点里）。

**顺带暴露并修掉的一个真冲突**：B3 的 `auto` 档会给"成功且已判定"的运行只落轨迹**骨架**，
而异步的轨迹**只存在于运行日志**——摘要了控制台就再也渲染不出 ReAct 时间线。现在 `auto`
按 `kind` 区分：同步（轨迹已在 HTTP 响应里交付）可摘要，**异步一律全文**。

**第 2 步（控制台改造）也已完成**：`web/lib/api/client.ts` 的 `api.investigate` 现在**两种模式
都认**——`POST` 回同步结果（含 `report`）就直接返回；回 `202 {job_id}` 就轮询
`.../jobs/{id}` 到终态，再取 `.../jobs/{id}/result`。取消 = abort：立刻停轮询，并**best-effort**
请后端取消 job。前端测试 +4（轮询到成功、同步直通、失败 job → `ApiError(timeout)`、abort → 停轮询并
触发 cancel）。

**第 3 步（翻默认）也已完成**：`settings.investigate_async()` 默认改为 **True**；测试由
`tests/conftest.py` 的 autouse 夹具统一钉成同步（要测异步的用例显式置 1，要测"默认值"的用例
`delenv`），新增回归测试 `test_default_is_async_when_env_absent` 钉住默认值。

**剩下的是部署注意项，不再是代码阻塞**：

1. ~~后端：异步完成态一次拿到 `verdict`/`trace`/`usage`~~ ✅（`_verdict_for` + `.../result`）；
2. ~~前端：控制台"提交 → 轮询 job → `.../result`"~~ ✅（双模式兼容 + 4 条前端测试）；
3. ~~再翻默认~~ ✅（默认 1；`?sync=1` 或 `=0` 退回同步；测试夹具钉同步 + 默认值回归测试）。

- 默认异步意味着**每个应用实例**都会起 worker 线程池 + 队列轮询线程（纯 API / 单副本实例可置
  `DD_INVESTIG@E_ASYNC=0`）；
- 多副本必须配 `DD_ST@E_BACKEND=postgres`（memory 后端的队列只在进程内）——`data-dog config check`
  会对"异步 + memory"报警告。

### A9 `telemetry_events` 分区 —— ✅ 走「摄取日桶」，三段全部落地并在**真 PG** 验证

**决策（已确认）**：接受"**跨天重投递可能多一行**"这一代价，走「摄取日桶」（D）。真库已验证机制：
应用维护的 `created_day` 普通列 + 主键 `(created_day, tenant_id, event_id)` + `PARTITION BY RANGE
(created_day)` 可建；同日重投递仍去重；`DROP TABLE` 昨日分区 0.009 s。**注意**：生成列不行——
PG 16 报 `Column "created_day" is a generated column.`（生成列做分区键是 PG 17 能力），所以是写路径
显式维护的普通列。

**第 1/3 步已落地**：schema + 写路径——`TelemetryEventRow.created_day`（`BigInteger`，进主键）
+ `models.day_of()`（唯一取整来源）+ `save_telemetry(..., now=...)` 由**同一时刻**算出
`created_at`/`created_day` + 迁移 `a9c1d2e3f4b5`（加列 → 回填 → 换主键，PG 用 `ALTER`、SQLite 走
batch；回填用 `floor` 而非 PG 的 double→int 四舍五入）+ 测试（同日幂等 / 跨天两行 / 回填 floor）。
**第 2 步已落地**：`partitioning.py` 改日粒度——分区键 `created_day`、DDL 边界为**日序号**、
`partition_end()` 仍返回 epoch（retention 比较逻辑未改）、真 PG 往返用例改日桶形态并加"落进物理
分区"断言。

**第 3 步已落地**：`convert_to_partitioned()` + `scripts/partition_telemetry.py --convert`
（`--dry-run` / `--keep-old`）——单事务建新分区父表 → 按既有数据月份建分区 → 拷数据 → 复制
`LIKE` **不复制**的 RLS/策略/授权 → 核对行数 → 改名切换 → 删旧表；失败整体回滚。真库踩到并修掉
两个坑：索引名 schema 级唯一（**先删旧表**再改回规范名）、主键要改**约束**名。

**真表端到端验证**（`data_dog_pytest` 的 `telemetry_events`，4 条 `-m postgres` 用例）：迁移（主键
加日桶）→ `--convert`（3 个分区、索引名回到 `pk_telemetry_events`/`ix_telemetry_tenant_created`、
RLS enable+force、策略 1 条、blockers 空）→ `save_telemetry` 的行**落进当月分区**（逐分区计数确认）
→ `drop_partitions_before` 把过期分区**整段 DROP**。

**此前的基础设施**（`46b344c`）：`storage/partitioning.py`（纯函数 + PG 探测 + 幂等建分区 + 只删整段过期
的分区）+ `scripts/partition_telemetry.py`（`--check` / `--ensure-months` / `--drop-before`）+
retention 自动分流到 `DROP PARTITION`。测试含 `-m postgres` 真库往返。

**本轮把 Docker 里的 PostgreSQL 起起来（`postgres:16-alpine`，端口 55432）后，两个测试在真库上
跑通，同时实测出一个此前没发现的硬阻塞**：

- PG 规则：**分区表的唯一约束必须包含全部分区键**。实测报错原文：
  `FeatureNotSupported: unique constraint on partitioned table must include all partitioning columns`；
- 而 `telemetry_events` 的主键是 `(tenant_id, event_id)`——它正是写入路径
  `INSERT ... ON CONFLICT DO UPD@E` 的**去重键**（`repository.py`）。所以"按 `created_at` 做
  范围分区 + 保留原主键"在 PG 上**不可能**；换 `PRIMARY KEY (tenant_id, event_id, created_at)`
  会让重复上报（同一 `event_id` 不同 `created_at`）不再被去重——**数据完整性代价**；
- 实测另一条路：`PARTITION BY HASH (tenant_id, event_id)` 可以保留该唯一约束，但**哈希分区
  无法按时间删分区**（各分区都混着所有年代），A9 想省的那笔 DELETE 一分也省不下来；
- 顺带修掉一个真错误：该表 `created_at` 是 `double precision`（epoch 秒），而上一版 DDL 生成的是
  时间戳字面量，真库里会报 `invalid input syntax for type double precision`。现在边界是 epoch 数值，
  并有真库用例钉住。

**已交付的防护**：`partitioning.check_partitionable()` **前置检查**——非 PG / 表不存在 / 分区键类型
不支持 / **唯一约束不含分区键** / 入向外键，逐条点名。`--check` 会打印阻塞项；`--convert` 类操作
在阻塞项存在时直接拒绝。真库回归用例 `test_real_telemetry_shape_is_blocked_by_its_primary_key`
用与 `telemetry_events` 同形的探针把"不行 + 被谁挡住"钉死。

**因此 A9 的正确表述**：分区**基础设施**可用且已真库验证；`telemetry_events` 的**转换需要一个
数据模型决策**（去重键怎么改、或接受哈希分区不省删除、或维持现状只靠 DELETE）——这不是工程
实现问题，是"重复上报去重"与"按年龄删分区"二选一的产品决策，不该由实现者替用户选。

**多副本收口（本轮）**：上面第 3 步的 `--convert` / `--ensure-months` / `--drop-before` 此前只有
"单事务 + 文档化的维护窗"。现在三个动作与"应用启动自动补齐"共用 `state/lease.py` 的
`partition:ddl` 单持有者租约（与 A6 的 `retention:purge` 同一把原语）：抢不到就**一个 DDL 都不执行**，
CLI 报 `skipped=lease_held` + `lease_owner` + **退出码 3**（不是 0 成功，也不是 2 配置错），启动路径
安静跳过并记下持有者；`--check` / `--dry-run` 不参与互斥（只读不抢锁）。边界如实写：**它不是转换
正确性的边界**——PG 表锁（`RENAME` 要 `ACCESS EXCLUSIVE`）才是，撞上只会被挡、不会坏数据；租约省掉
的是并发白干与"谁在动结构"说不清。TTL 必须大于最慢的那次 DDL（`DD_PARTITION_DDL_LEASE_TTL_S` 默认
900s）；`config check` 对"自动补分区 + memory 后端"与"自动补分区 + 关租约"两条危险组合报警。

**转换的两道机器可验前置条件（本轮收口互斥管不到的那一半）**：互斥只保证"同一时刻一个入口在动
结构"，与"有人正在**写**"无关；而 `RENAME` 前的 `INSERT INTO new SELECT * FROM old` 拿的是读快照，
**事务期间的并发写入不会进新表**，只能靠 runbook 里一句"执行前请停止接入"——那是靠人记得。现在：
`--confirm-table <表名>`（原样复述目标表名，与 `POST /v1/tenant/purge` 的 `X-Confirm-Tenant` 同意）与
`--require-quiet N`（最近一次写入必须早于 N 秒前，在**拿锁之后、任何 DDL 之前**、`INSERT...SELECT` 同一事务内检查）
都是库层硬门（`partitioning.check_convert_confirm` / `check_convert_quiet`），不满足即拒绝、退出码 2；
`--dry-run` 只报 `quiet.last_write_age_s` 不拒绝。**边界如实写**：它把"以为停了"变成"实测停了"，
**不能阻止静默窗口之后新到的写入**，也不是安全边界。另外转换事务里的 PG 咨询锁改为
`pg_try_advisory_xact_lock`（`lock_wait=False` 默认）：抢不到就 `ConversionBusy` + `skipped=convert_busy` + 退出码 3，
**不排队**——会话上的 `statement_timeout`（15s）会把"排队"变成看不懂的 `QueryCanceled`（迁移入口仍排队，因为迁移不可分割）。

### A10 控制台收敛 + 多租户身份（已落地：收敛 + 租户选择 + 用户级授权最小步；OIDC 为边界）

**已落地（本轮）**：

1. **收敛决定写进文档**：产品控制台 = `web/`（Next.js，服务端代理、测试齐全）；`ui/`
   （Streamlit）定位为**演示台**，不再往上加产品功能（README 目录说明 + 快速开始 + `docs/delivery.md`）。
2. **多租户的"选择"最小步**：`DD_WEB_TENANTS` 服务端**允许清单** + `dd_web_tenant` cookie。
   默认（未配清单）行为**逐字不变**——cookie 被完全忽略；配了清单则只允许在清单内选，
   清单外 **403 失败关闭**（不回退到默认租户）；浏览器自带的 `X-Tenant-Id` 头**始终**被忽略。
   前端测试 +6（默认忽略 cookie / 清单内可选 / 清单外 403 / 头仍被忽略 / 转发前即拒 / 清单解析）。

3. **用户级授权最小步**：`DD_WEB_TRUST_PROXY_AUTH=1` + `DD_WEB_USER_TENANTS="alice:a,b;bob:b"`
   ——可信反代注入用户头（默认 `x-auth-request-user`，可配），控制台强制"用户只能碰自己的租户"：
   缺头 / 未知用户 / cookie 越权**一律 403**（失败关闭，不给默认权限）。前端测试再 +7。

**仍是人工边界（不假装做完）**：

- **没有登录页 / 用户目录 / OIDC**：身份完全由反代注入（`DD_WEB_TRUST_PROXY_AUTH`），控制台
  不做登录、不改密码、不管用户生命周期；`DD_WEB_USER_TENANTS` 是**配置文件里的静态映射**，不是
  授权数据库。
- **服务端仍无用户级授权**：后端只认服务端强制的 `X-Tenant-Id`（没有"用户"概念）。所以用户级
  约束是**控制台这一层**的，直连后端（绕过控制台）不受它约束——生产必须保证后端不可被绕过访问。
- **反代必须剥离客户端伪造的用户头**：这是启用该开关的**硬前提**，写进了 CLI 无法检查的部署项里
  （`web/lib/api/proxy.ts` 的 `resolveIdentity` docstring 与 `docs/delivery.md` 都写明）。
- 因此控制台的租户隔离**目前仍依赖"受信网关 + 服务端配置"**（与 `docs/delivery.md` 的多租户
  行一致）。

## 顺带修掉的阻塞项与追加项（不在 A/B 清单内）

| 提交 | 问题 | 为什么必须修 / 做了什么 |
|---|---|---|
| `49bd1d0` | A3 的数据块把确定性评测"看瞎"了 | 全量测试 **10 failed**：工具返回被包成数据块后 `json.loads` 失败，裁决静默退化成 `needs_human_review`。加 `core/injection.unwrap` 剥壳 + 3 条回归测试 |
| `6e27584` | 审计 `ts` 在 Windows 上分辨率不足（~15 ms） | `test_audit.py` 单跑 5 次红 3 次：同一 `ts` 内顺序由随机 `audit_id` 决定。改为进程内严格递增 + 回归测试 |
| `6cb38eb` | "僵死 job"用例卡在阈值边界上 | `investigate_stale_running_s()` 有 **10s 下限**，测试写 `=1` 实际是 10，而种子用 `age_s=10` → 是否跨过一个时钟 tick（~15.6ms）成了随机事件，全量跑约 20% 概率红。种子改 30s + 把"下限 10s"钉成回归测试 |
| `7f71020` | HTTP 推送对端半途断开 → handler 线程未捕获异常 | 全量跑约 1/3 红（`ConnectionAbortedError: [WinError 10053]`）。客户端断开是**正常现象**：`do_POST` 兜住 `(ConnectionError, TimeoutError)` 记 debug 后返回，不吞其它异常 |
| `32331fe` | 同步调查"模型慢还是我们慢"分不开 | 新增 `llm_request_duration_seconds{vendor,model,outcome}`（桶到 120s）；`Provider.chat()` 的 `finally` 里统一观测，**失败/超时也进**（只统计成功会低估 P95） |
| `4cde4c4` | `rawlog://` 证据源是 **mock**（`sha256:mock`） | ref 能解析、内容却是假的。新增真读（受控目录 / 对象存储二选一）、未配置即**失败关闭**、超限截断标注、读取留痕 `audit action=rawlog.read`；`--check`/config check 把关互斥 |
| `af74857` | 目录读取的 TOCTOU + http 截断靠"猜" | POSIX 逐段 `openat`+`O_NOFOLLOW`（任何符号链接都拒，容器实测）；Windows 退回 `resolve()` 检查并**明写**仍有窗口。截断判定改为确定（按「上限+1」取） |
| `35cd015` | `safe_request(max_bytes=N)` 被传输层 64 KiB 卡死 | 上层以为放宽了上限，实际 `_default_transport` 写死读 `_MAX_BYTES`。`PinnedRequest` 带上 `max_bytes`，真起本地 HTTP 服务验 100 KiB 完整取回 |
| `cdb13fd` | 控制台没消费 `/refs`（引用只是字符串数组） | 候选详情加「证据引用」面板：**懒解析**（点按钮才请求，因读正文会留审计）、展开看正文/截断/摘要、失败显示错误码；devmock/fixtures 的 `evidence://` 换成真 scheme |
| `e434e91` | knowledge flake 只有一句"无响应"，无法定位 | 握手改用独立启动预算（`initialize` 180s，其余 60s——负载重时的冷启动此前会被误判挂起并 kill）；stderr 落临时文件；报错带上"看门狗 kill/自行退出 + 退出码 + stderr 尾巴"。**未能复现**原 flake，故只声称"可诊断 + 消除负载敏感性" |

## 性质测试：安全边界的"全称命题"（不在 A/B 清单内）

A/B 各项的证据此前都是**例子测试**（"这几个路径被拒了"）。例子能证明"有防护"，证明不了
"没有缺口"——攻击面恰好是没人想到的输入。故给四处安全边界补了性质测试
（`tests/test_properties_security.py`，hypothesis，`derandomize=True` 保持确定性与离线）：

| 性质（全称命题） | 覆盖的边界 |
|---|---|
| 任意文本 → 解析器**永不抛**，失败必须收敛成结构化错误码 | 引用解析对外契约 |
| `is_valid_ref` 与 `parse_ref` 完全同口径；白名单外 scheme 一律不合法 | 引用语法（防绕过） |
| 任意存储键 → rawlog 目录读取器：或读到**根内**文件、或被显式拒绝；**根外哨兵内容永不出现** | 容器化（防路径穿越） |
| 任意环境变量字典 → 配置体检与渲染都**不抛**，结构始终成立 | 部署前体检（崩了没人用） |
| 任意浮点时间 → `day_of` 单调且 `[d*86400,(d+1)*86400)` 恰好包住输入 | 分区键算术（差一就落错分区） |
| 任意标识符 → 分区 DDL 要么被白名单接受、要么 `ValueError`，**绝不**拼进 SQL | DDL 注入 |

**当场抓到一个真缺口**（这正是补它的理由）：存储键里含 NUL 字节（`"\x00"`）时，文件系统层抛的是
`ValueError: stat: embedded null character in path`，不是约定的 `RawLogRefused`——手写用例想得到
`../../`，想不到 NUL。已收敛：`rawlog.make_rawlog_source` 把 `OSError/ValueError/UnicodeError`
统一映射成 `RawLogRefused`（附原因），不再让非法键冒成 500。

## 可观测缺口补齐：告警别再"缺条腿"（不在 A/B 清单内）

上一轮补了 LLM 延时直方图与分区有效期，但**告警侧没跟上**——指标有了、没人被叫醒等于没有。
本轮按 `docs/slo.md` 自己的标准（"不会响的 SLO 只活在文档里"）查了一遍缺口，补 5 条：

| 告警 | 指标 / 口径 | 为什么必须有 |
|---|---|---|
| `DataDogLlmLatencyP95High` | `histogram_quantile(0.95, sum by (le)(rate(llm_request_duration_seconds_bucket[10m])))` | 上一轮刚加的直方图此前**没有任何规则**；失败/超时也计入，只统计成功会把 P95 低估 |
| `DataDogLlmFailureRateHigh` | 超时+不可用 rate / 全部 rate | LLM 失败会 **fail-closed**（判定降级为 needs_human_review），超标 = 人在替模型兜底 |
| `DataDogOutboxBacklog` | `max(db_rows{table="outbox_pending"})` | "告警产生了但送不出去"此前无人知晓；与审计写失败互补 |
| `DataDogEvidenceResolveFailed` | 新增计数器 `evidence_resolve_total{outcome="execution_failed"}` | 数据源没配好/挂了（如 rawlog 未配置）此前只表现为"用户打不开原始日志" |
| `DataDogEvidenceDeniedSpike` | `evidence_resolve_total{outcome="denied"}` | 正常应为 0；涨起来是**越权探测**信号 |

配套改动：`core/metrics.py` 新增 `data_dog_evidence_resolve_total{outcome}`，
在解析器**唯一出口**观测（`EvidenceResolver.resolve` 包一层，内部逻辑一字未改）；
计数器**永不抛**——观测不能成为链路的一环。新增 4 个阈值进 `values.yaml`，
5 行进 `docs/slo.md` §9 表（既有不变量测试会强制"规则↔指标↔阈值↔文档"四向对齐）。

## 覆盖率门禁：从"总分"到"分腿、分文件"（不在 A/B 清单内）

问题有两层，第二层比第一层重要：

1. **门槛松**：CI 是总分 `--cov-fail-under=85` + 单文件 `cov_gate.py --min 60`。
   60% 对核心文件等于没门（实测最低文件 35.4%）。
2. **口径错**：默认腿跑在 SQLite 上，PG-only 的主体在它眼里覆盖率极低——
   `storage/partitioning.py` 在默认腿 **35.4%**、在 postgres 腿 **86.7%**（同一份代码、同一批测试）。
   照着默认腿的数字调门槛，只会把"口径不对"误判成"没测"，逼人去写假的覆盖。

**做法**：

1. `cov_gate.py` 下限默认 60 → **70**，新增两个开关：
   - `--only`：只检查匹配路径片段的文件（把 PG-only 文件划给 postgres 腿）；
   - `--no-allow`：忽略 ALLOW 豁免——跨腿门禁**必须**用它，否则文件在两条腿上
     都被豁免，等于没人看；
   - 给 `storage/partitioning.py`、`state/pg.py` 写明豁免理由（口径不适用，附两条腿实测数字）。
2. 分腿设门槛（本机真跑，数字如下）：

| 腿 | 命令 | 结果 |
|---|---|---|
| 默认（SQLite） | `cov_gate --json coverage.json --min 70` | ✅ 通过（总覆盖 **87.94%**） |
| postgres | `cov_gate --json coverage-pg.json --only storage/partitioning.py,state/pg.py --no-allow --min 80` | ✅ 通过（**86.7% / 98.5%**） |
| 反证（门槛调 90） | 同上 | ✗ 红（86.7% < 90）——门禁确实咬得住，不是摆设 |

3. `make cov-gate` / `make cov-gate-pg` 把两条命令固定下来（不靠记忆敲参数）。

**已接线**（`211c5a6`）：默认腿 `--min 70`；postgres 腿加 `--cov` 并新增 PG-only 文件门禁
（`--only storage/partitioning.py,state/pg.py --no-allow --min 80`）。

提交方式值得记一笔：`.github/workflows/ci.yml` 当时**正被另一个窗口编辑**（Playwright e2e 腿，
+40 行），直接提交该文件会夹带对方在途改动；于是用 git plumbing
（`read-tree` / `update-index` / `commit-tree` / `update-ref`，带旧值 CAS）构造
"HEAD + 仅本补丁"的提交，全程不碰工作区与真实 index，事后把同样补丁叠加回工作区文件。
验收：`git diff -- .github/workflows/ci.yml` 只剩对方那 40 行新增、**零删除**（我的补丁没被顶掉），
对方的暂存文件也原样保留。

## 含金量收口：提交一览（不在 A/B 清单内）

| 项 | 提交 | 一句话 |
|---|---|---|
| ① A9 生产最后一公里 | `b204ec6` | 分区有效期**时间戳**指标 + 2 条告警 + 2 个 CronJob + 启动兜底 + `make partition`（真库验证） |
| ③ 性质测试 | `72b4156` | hypothesis 全称命题打安全面；**当场抓到 NUL 键缺口**并收敛 |
| ④ 告警缺口 | `85c1382` | 5 条告警 + 证据解析计数器（指标↔阈值↔文档**四向对齐**） |
| ⑤ 覆盖率门禁 | `bc1dce5` `211c5a6` `6143866` | 单文件 60%→70% + PG-only 文件分腿 80%；**反证**验证；CI 已接线 |
| ② 控制台 E2E | `635327d` | 真 Chromium 走「代理 → 租户 → 异步轮询 → refs 面板」；**抓出"候选行点不动"的真 bug**（裸 CSS 压掉工具类） |
| — | A9 多副本 DDL 互斥 | 本轮 | 分区"建 / 删 / 转"与启动自动补齐共用 `partition:ddl` 单持有者租约；抢不到**一个 DDL 都不执行**、退出码 3 + `skipped=lease_held`（不静默成功） |

② 的实现来自并行窗口，本轮由我**独立验收后接手提交**（tsc/lint/vitest/e2e 四项按 CI 口径全绿）。

**最终验证（本轮末）**：全量 `1368 passed / 20 skipped`；PG 腿 `27 passed`；
web：`tsc --noEmit` 0 错误、`next lint` clean、`vitest` 102 passed、
`npm run e2e` 7 passed。

## 本轮：三个"含金量"缺口（Windows TOCTOU / 多副本硬互斥 / 性能基线）

| 缺口 | 提交 | 关键点 |
|---|---|---|
| A. Windows 下 rawlog 的 TOCTOU | `486eadc` | `CreateFileW(FILE_FLAG_OPEN_REPARSE_POINT)`（不跟随最后一段）+ `GetFinalPathNameByHandleW` **核对已打开句柄**的真实路径；4 条 Windows 专测。**"根内 junction 别名"是新旧行为分水岭**：旧实现 resolve 后做包含性检查会放行，新实现看句柄事实 → 拒。残留边界只写**同卷硬链接**（同一个文件，本不该区分） |
| B. 多副本/多入口硬互斥 | `d5ed1e9` | `storage/pg_locks.py`：迁移用**会话级**锁（独立连接、跨 alembic 多事务）、分区转换用**事务级**锁（自动释放）。与并行窗口的 `partition:ddl` **租约分工**：租约依赖状态表、可查"谁在动手"；咨询锁不依赖任何表，覆盖**迁移**（表还没建）与**租约 TTL 过期而长 DDL 未完**两处结构缺口。真库 5 条用例 + 端到端迁移实测 |
| C. 性能基线与代码对齐 | `e76c5c7` | `tools/bench.ps1`（无 bash 时的等价复现；UTF-8 **带 BOM**，否则 PS 5.1 按 ANSI 读会因中文注释直接语法报错）+ 5 组实测 + `tests/test_bench_baseline.py`（**声明==事实**不变量） |

**C 的实测结论（值得单独记一笔）**：

- 写队列（T1）把 SQLite 写 P99 **2296ms → 217ms**、吞吐 201 → **300 req/s**（打满目标）；
  PG 写 P99 **752 → 507ms**（仍贴着 SLO 500ms）。它是"把同步落库移出请求路径"，不是"写得更快"。
- **默认队列 1000 在 60 并发 ×300rps ×15s 下拒收 21.6%（970×503）**——设计内的失败关闭；
  队列 5000 → **0 错误、P99 221ms**。即**写线程跟得上（≥300rps），瓶颈是队列容量**：生产必须
  显式定容（已写进基线的"下一步"）。
- 五种配置共同满足：双侧一致（≤0.1%）、**热路径零 LLM**（调查计数增量 0）。

**C 的不变量测试当场抓到过期**：基线把"写入路径批量化落库"列为未完成的下一步，而写队列早已
实施（`service/write_behind.py` / `DD_WRITE_BEHIND`）——这条测试**先红后绿**，红的时候正是要修的
那个过期项。"能自动核对的事实，就别指望人记得同步"在本仓又一次生效。

**边界（本轮如实标注）**：B 的契约文档（`docs/storage.md` 的多副本一节）由并行窗口的租约改动一并落
（两边同时在改该文件，本提交不去撞），锁的机制与分工写在与代码同址的模块 docstring 里；
A 的 Windows 保证形式与 POSIX **不等价**（POSIX 把"检查+打开"合成同一个系统调用，Windows 是
"一次打开 + 对已打开对象核路径"），差异写进 docstring；C 的数字是**下界**（本机另有 agent 在跑）。

## 怎么复核

```bash
git log --oneline -12                    # 逐项提交
pytest tests/test_retention.py tests/test_run_log.py tests/test_helm_alerts.py \
       tests/test_config_check.py tests/test_injection_defense.py tests/test_audit.py \
       tests/test_partitioning.py tests/test_async_investigation.py tests/test_job_atomicity.py -q
python scripts/partition_telemetry.py --check   # A9：分区现状（非 PG 会明确说不可用）
data-dog config check                    # B4：配置体检（也可 make config-check）
python scripts/readme_counts.py --check  # B5：README 规模数字（pre-commit 已挂）
```
