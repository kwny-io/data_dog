# 架构评审清单 A/B：落地台账（2026-10-14）

> 来源：以"高级软件架构师"视角对**代码事实**（不是文档）做的评审：A 为真缺口 10 条、
> B 为存量可优化 7 条。本文件是**逐项状态**：做完了什么、证据在哪、**没做的边界与原因**。
> 纪律：每项**独立提交**、带回归测试、文档同步；无法安全完成的项**写明边界**，不含糊过去。

## 总览

| 分组 | 已落地 | 未落地（见下"边界"） |
|---|---|---|
| A（10 条缺口） | A1 A2 A3 A4 A5 A6 A7 A8 | **A9 A10** |
| B（7 条优化） | B1 B3 B4 B5 B6 B7 | **B2** |

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
| A9 | `telemetry_events` 没有分区 | ❌ | — | 见下"未落地项" |
| A10 | 控制台仍单租户 + 两套 UI | ❌ | — | 见下"未落地项" |

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

### B2 同步默认翻异步（`DD_INVESTIGATE_ASYNC`）

**没翻，因为按现状翻会是一次功能回退**——三条代码事实：

1. 控制台消费的是**同步响应**的完整载荷：`web/components/InvestigationPanel.tsx` 要渲染
   `verdict` / `trace` / `usage` / `alerting`（`web/lib/api/client.ts` 的 `investigate`）。
2. **异步路径不产出同等载荷**：`JobRunner._finish` 只落 report + run_log；
   `GET /v1/investigations/jobs/{id}` 只回 job 行（`_job_public`），`GET /v1/investigations/{id}`
   只回 report。`trace`/`usage` 要另取运行日志，而 **`alerting` 在异步路径无处可取**。
   → 直接翻默认（或让控制台改轮询）会让控制台丢掉结论与轨迹展示。
3. 翻默认还有**进程级副作用**：`create_app` 按 `investigate_async()` 决定是否起 worker 池
   （`service/runtime.py`）——默认翻开会给每个应用实例加一个线程池 + 队列轮询线程。

**可做的下一步（按序，缺一不可）**：

1. 后端：让异步完成态一次拿到 `verdict` / `alerting`（补字段或新端点）；
2. 前端：控制台改"提交 → 轮询 → 取报告"，并补 MSW/vitest 与取消语义；
3. 再翻默认，并把同步变成显式 `?sync=1`（或留一个发布周期双轨）。

现状已被测试钉住：`tests/test_async_investigation.py`（未开 → 200 + 完整报告，旧契约）——
未来翻默认时它会红，提醒执行上面三步，而不是悄悄改变契约。

### A9 `telemetry_events` 分区

**没做，两个硬原因**：

1. **本机没有 PostgreSQL**（`127.0.0.1:5432` 实测 `ConnectionTimeout`）→ 分区 DDL 无法在本环境
   验证。本仓的纪律是"没在真 PG 上验证过的不算落地"（A5 就是先在真 PG 16 上验完才写的）。
2. **把既有表转成分区表在 PG 上不能原地做**：要新建分区父表 + 拷数据 + 建索引/外键 + 改名切换，
   属于**需要停机窗口**的迁移。评审自己也把这条列为"大 / 要单独立项"。

**可做的下一步**：`storage/partitioning.py`（`ensure_partitions` / `drop_partitions_before`）
+ 新装路径直接建分区父表 + retention 在"已分区"时改走 `DROP PARTITION`（否则回退分批 DELETE）
+ PG 测试腿上的 `-m postgres` 用例 + **既有表转换 runbook**（含回滚）。

### A10 控制台收敛 + 多租户身份

**没做，因为这是产品决策而不是工程修复**：收敛到 `web/`（Next.js）还是 `ui/`（Streamlit）、
身份体系用哪个（OIDC / 反代头 / 自建用户存储），需要产品负责人拍板；在拍板前删改 UI 等于
替用户做选择。文档现状已如实写明"单租户控制台 + 无用户级 RBAC"（`docs/delivery.md`）。

**可做的下一步（拍板后）**：① 保留 `web/` 为产品控制台、把 `ui/` 标为 demo-only 并移出主路径；
② 身份接 OIDC（或受信反代头）；③ 控制台按租户切换 + 用户级 RBAC。

## 顺带修掉的两个阻塞项（不在 A/B 清单内）

| 提交 | 问题 | 为什么必须修 |
|---|---|---|
| `49bd1d0` | A3 的数据块把确定性评测"看瞎"了 | 全量测试 **10 failed**：工具返回被包成数据块后 `json.loads` 失败，裁决静默退化成 `needs_human_review`。加 `core/injection.unwrap` 剥壳 + 3 条回归测试 |
| `6e27584` | 审计 `ts` 在 Windows 上分辨率不足（~15 ms） | `test_audit.py` 单跑 5 次红 3 次：同一 `ts` 内顺序由随机 `audit_id` 决定。改为进程内严格递增 + 回归测试 |

## 怎么复核

```bash
git log --oneline -12                    # 逐项提交
pytest tests/test_retention.py tests/test_run_log.py tests/test_helm_alerts.py \
       tests/test_config_check.py tests/test_injection_defense.py tests/test_audit.py -q
data-dog config check                    # B4：配置体检（也可 make config-check）
python scripts/readme_counts.py --check  # B5：README 规模数字（pre-commit 已挂）
```
