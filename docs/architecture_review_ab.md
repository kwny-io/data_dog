# 架构评审清单 A/B：落地台账（2026-10-14）

> 来源：以"高级软件架构师"视角对**代码事实**（不是文档）做的评审：A 为真缺口 10 条、
> B 为存量可优化 7 条。本文件是**逐项状态**：做完了什么、证据在哪、**没做的边界与原因**。
> 纪律：每项**独立提交**、带回归测试、文档同步；无法安全完成的项**写明边界**，不含糊过去。

## 总览

| 分组 | 已落地 | 未落地（见下"边界"） |
|---|---|---|
| A（10 条缺口） | A1–A10 全部落地 | —（A10 的身份体系为人工边界，见下） |
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
| A9 | `telemetry_events` 没有分区 | ✅ | 见下 | `storage/partitioning.py` + 维护 CLI（建/删分区）+ retention 自动分流到 `DROP PARTITION`；**首次转换是需停机窗口的人工 runbook**，真库往返由 CI 的 `-m postgres` 腿覆盖 |
| A10 | 控制台仍单租户 + 两套 UI | ✅ | 见下 | 收敛决定：**产品控制台 = `web/`，`ui/` 只作演示台**（README/交付文档写明）；多租户最小步：`DD_WEB_TENANTS` 服务端**允许清单** + `dd_web_tenant` cookie 做租户**选择**（清单外 403、未配置时行为不变）。**用户级身份/RBAC 仍是人工边界** |

## B. 存量可优化

| # | 优化项 | 状态 | 提交 | 落地内容（一句话） |
|---|---|---|---|---|
| B1 | 同步路 histogram 顶格 2.5s → 全落 +Inf | ✅ | `51091f1` | 桶补到 120s（异步路另有 job 直方图），同步调查 P95/P99 重新可观测 |
| B2 | 同步仍是默认 | ❌ | — | 见下"未落地项" |
| B3 | 运行日志写放大（40 步 ≤160 KB/次） | ✅ | `8213327` | `DD_RUN_LOG_FULL_STEPS=auto`：成功且已判定只落**骨架**，失败/取消/`needs_human_review` 落全文；`steps_truncated` 如实标注 |
| B4 | 配置治理：60+ `DD_*` 无启动期校验 | ✅ | `0e785fe` | `data-dog config check` / `make config-check`：未知键、非法取值（会静默回默认）、危险组合；不连库、有错即非零退出 |
| B5 | meta 测试靠人记得跑 | ✅ | `17f775a` | `readme_counts.py --check` 挂进 pre-commit，且**无条件**跑（漂移常发生在没有 .py 变更的提交里） |
| B6 | `run_<uuid16>` 可读性 | ✅ | `9caeda2` | `run_<候选缩写>_<UTC时间戳>_<4位hex>`（4 位随机尾防同一秒重跑撞主键覆盖日志） |
| B7 | 文档断言缺自动核对 | ✅ | `51091f1` | 两条 meta 测试把"声明 == 事实"钉住：逐列按 PG 方言编译检查 + 迁移清单与模型元数据逐条比对 |

## 未落地项：边界与原因

### B2 同步默认翻异步（`DD_INVESTIGATE_ASYNC`）——✅ 三步全部落地

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

### A9 `telemetry_events` 分区

**没做，两个硬原因**：

1. **本机没有 PostgreSQL**（`127.0.0.1:5432` 实测 `ConnectionTimeout`）→ 分区 DDL 无法在本环境
   验证。本仓的纪律是"没在真 PG 上验证过的不算落地"（A5 就是先在真 PG 16 上验完才写的）。
2. **把既有表转成分区表在 PG 上不能原地做**：要新建分区父表 + 拷数据 + 建索引/外键 + 改名切换，
   属于**需要停机窗口**的迁移。评审自己也把这条列为"大 / 要单独立项"。

**可做的下一步**：`storage/partitioning.py`（`ensure_partitions` / `drop_partitions_before`）
+ 新装路径直接建分区父表 + retention 在"已分区"时改走 `DROP PARTITION`（否则回退分批 DELETE）
+ PG 测试腿上的 `-m postgres` 用例 + **既有表转换 runbook**（含回滚）。

### A10 控制台收敛 + 多租户身份（部分落地：收敛 + 租户选择；身份/RBAC 为人工边界）

**已落地（本轮）**：

1. **收敛决定写进文档**：产品控制台 = `web/`（Next.js，服务端代理、测试齐全）；`ui/`
   （Streamlit）定位为**演示台**，不再往上加产品功能（README 目录说明 + 快速开始 + `docs/delivery.md`）。
2. **多租户的"选择"最小步**：`DD_WEB_TENANTS` 服务端**允许清单** + `dd_web_tenant` cookie。
   默认（未配清单）行为**逐字不变**——cookie 被完全忽略；配了清单则只允许在清单内选，
   清单外 **403 失败关闭**（不回退到默认租户）；浏览器自带的 `X-Tenant-Id` 头**始终**被忽略。
   前端测试 +6（默认忽略 cookie / 清单内可选 / 清单外 403 / 头仍被忽略 / 转发前即拒 / 清单解析）。

**仍是人工边界（不假装做完）**：

- **没有用户级身份与 RBAC**：`DD_WEB_TENANTS` 是租户**选择**，不是**授权**——谁能访问控制台仍
  由 Basic 门 / 认证反代承担。要真正的"某个用户只能看某个租户"，需要引入 OIDC（或受信反代头）
  + 用户-租户授权表；这是产品与安全决策，不是能顺手补的代码。
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
