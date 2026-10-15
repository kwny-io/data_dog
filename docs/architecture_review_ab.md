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
| A2 | 队列无准入/背压 | ✅ | `b0142c8` | 共享队列深度 + `DD_INVESTIGATE_QUEUE_MAX`；超限 **落 job 之前** 429 + `Retry-After`（不做假受理） |
| A3 | Prompt 注入只有 mock 回归 | ✅ | `afd62f4` `49bd1d0` | 不可信文本包成数据块 + 系统提示声明指令层级 + 注入特征进 trace；红队入口 `make redteam`。后续修掉数据块导致的**评测静默失效**（`unwrap` 剥壳） |
| A4 | 提交侧无幂等 | ✅ | `b0142c8` | `Idempotency-Key`：窗口内同 (租户,key) 只真跑一次；重放带 `idempotent_replay`；进行中 409 + `Retry-After` |
| A5 | PG 上 JSON 列是 `json` 不是 `jsonb` | ✅ | `51091f1` | 方言变体 + 迁移 `f2a3b4c5d6e7` ALTER 既有库；真 PG 16 验证 24/24 列 `jsonb`、可逆 |
| A6 | 多副本下 retention 无租约 | ✅ | `bf75343` | 真删前抢 `retention:purge` 单持有者租约；抢不到本轮**跳过**并如实标注（≠ 删了 0 行） |
| A7 | 没有告警规则 / SLO 未落地 | ✅ | `574b384` | `PrometheusRule` 12 条规则（可用性/热路径 P99/治理积压/静默失败）+ `docs/slo.md` §9 映射表 + 不依赖 helm 的 meta 测试 |
| A8 | LLM 无降级链 | ✅ | `b0142c8` | 有界重试（只重试可重试错、带抖动、每次重过 deadline）+ 备用模型；`DD_LLM_RETRIES` / `DD_LLM_FALLBACK_MODEL` |
| A9 | `telemetry_events` 没有分区 | ✅ | `46b344c` + 本轮 | 走「摄取日桶」（代价：跨天重投递多一行，有检测函数）：schema/写路径/迁移 → 分区模块日粒度 → 转换脚本；三段均在**真 PG** 验证（含真表转换 + 写路径落分区 + 整段 DROP），见下 |
| A10 | 控制台仍单租户 + 两套 UI | ✅ | 见下 | 收敛决定：**产品控制台 = `web/`，`ui/` 只作演示台**（README/交付文档写明）；多租户最小步：`DD_WEB_TENANTS` 服务端**允许清单** + `dd_web_tenant` cookie 做租户**选择**（清单外 403、未配置时行为不变）。**用户级身份/RBAC 仍是人工边界** |

## B. 存量可优化

| # | 优化项 | 状态 | 提交 | 落地内容（一句话） |
|---|---|---|---|---|
| B1 | 同步路 histogram 顶格 2.5s → 全落 +Inf | ✅ | `51091f1` | 桶补到 120s（异步路另有 job 直方图），同步调查 P95/P99 重新可观测 |
| B2 | 同步仍是默认 | ✅ | `d3b5f9d` `38751dc` `473a3d9` | 三步：① 异步结果一次取齐 `report/verdict/trace/usage`（同源 `_verdict_for`）；② 控制台改"提交→轮询→取结果"；③ `DD_INVESTIGATE_ASYNC` **默认 1**（测试用 conftest 钉成同步，默认值另有回归）；见下 |
| B3 | 运行日志写放大（40 步 ≤160 KB/次） | ✅ | `8213327` | `DD_RUN_LOG_FULL_STEPS=auto`：成功且已判定只落**骨架**，失败/取消/`needs_human_review` 落全文；`steps_truncated` 如实标注 |
| B4 | 配置治理：60+ `DD_*` 无启动期校验 | ✅ | `0e785fe` | `data-dog config check` / `make config-check`：未知键、非法取值（会静默回默认）、危险组合；不连库、有错即非零退出 |
| B5 | meta 测试靠人记得跑 | ✅ | `17f775a` | `readme_counts.py --check` 挂进 pre-commit，且**无条件**跑（漂移常发生在没有 .py 变更的提交里） |
| B6 | `run_<uuid16>` 可读性 | ✅ | `9caeda2` | `run_<候选缩写>_<UTC时间戳>_<4位hex>`（4 位随机尾防同一秒重跑撞主键覆盖日志） |
| B7 | 文档断言缺自动核对 | ✅ | `51091f1` | 两条 meta 测试把"声明 == 事实"钉住：逐列按 PG 方言编译检查 + 迁移清单与模型元数据逐条比对 |

## 未落地项：边界与原因

### B2 同步默认翻异步（`DD_INVESTIGATE_ASYNC`）——✅ 三步全部落地（`d3b5f9d` `38751dc` `473a3d9`）

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
  `DD_INVESTIGATE_ASYNC=0`）；
- 多副本必须配 `DD_STATE_BACKEND=postgres`（memory 后端的队列只在进程内）——`data-dog config check`
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
  `INSERT ... ON CONFLICT DO UPDATE` 的**去重键**（`repository.py`）。所以"按 `created_at` 做
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

## 顺带修掉的三个阻塞项（不在 A/B 清单内）

| 提交 | 问题 | 为什么必须修 |
|---|---|---|
| `49bd1d0` | A3 的数据块把确定性评测"看瞎"了 | 全量测试 **10 failed**：工具返回被包成数据块后 `json.loads` 失败，裁决静默退化成 `needs_human_review`。加 `core/injection.unwrap` 剥壳 + 3 条回归测试 |
| `6e27584` | 审计 `ts` 在 Windows 上分辨率不足（~15 ms） | `test_audit.py` 单跑 5 次红 3 次：同一 `ts` 内顺序由随机 `audit_id` 决定。改为进程内严格递增 + 回归测试 |
| `6cb38eb` | "僵死 job"用例卡在阈值边界上 | `investigate_stale_running_s()` 有 **10s 下限**，测试写 `=1` 实际是 10，而种子用 `age_s=10` → 是否跨过一个时钟 tick（~15.6ms）成了随机事件，全量跑约 20% 概率红。种子改 30s + 把"下限 10s"钉成回归测试 |

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
