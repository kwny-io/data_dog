# 调查运行日志（Run Log）：让技术员**事后**能调出"这次诊断做了什么"

> 面向：值班人 / 技术支持 / 事故复盘。它回答的是**第三个问题**——不是"进程刚才干了什么"，
> 也不是"谁按了按钮"，而是"**这一次诊断，Agent 查了什么、凭什么这么判、失败卡在哪**"。

## 0. 先分清三份"日志"（它们不互相替代）

| 想回答的问题 | 用哪份 | 存哪 | 保留 |
|---|---|---|---|
| 进程/请求刚才发生了什么、有没有报错 | **结构化运行日志**（JSON lines，带 `request_id`/`trace_id`） | stdout → 外部采集（Loki/ELK/CloudWatch） | 由采集侧决定 |
| 谁在何时对什么做了什么、结果如何 | **安全审计**（`audit_log` + `GET /v1/audit`） | 库表，**只增不改** | `DD_RETENTION_AUDIT_DAYS`（默认 0 = 永久） |
| **这次诊断的过程与失败原因** | **调查运行日志**（本文，`investigation_run_log`） | 库表，按租户隔离 | `DD_RETENTION_RUN_LOG_DAYS`（默认 90 天） |

为什么非要单独一份：审计里没有"第 3 步工具返回了什么"；进程日志里没有业务上下文
（哪个候选、哪次调查），而且它**默认不落库**（只有外部采集才有），跨副本还分散在各进程 stdout。

## 1. 什么时候写

**每一个出口都写**——成功写，失败/取消**也写**：

| 路径 | `run_id` | 谁写 | 落库时机 |
|---|---|---|---|
| 同步调查成功 | `investigation_id`（报告 id） | `service/runtime._execute_investigation` | 报告落库后立刻 |
| 同步调查失败 | `run_<hex16>`（本次尝试） | 同上 | 立刻（HTTP 仍是 4xx/5xx） |
| 异步调查（成功/失败/取消） | `job_id` | `JobRunner._finish` | 与 **job 终态同事务** |

两条硬约定：

1. **异步路径"job 有终态 ⇔ 有运行日志"**：日志与 job 状态、报告在**同一个事务**里写。
   执行侧连日志都没带回来（例如它自己抛了异常），`_finish` 会补一条骨架——
   宁可日志少内容，不可"任务失败了但什么都没有"。
2. **同步失败没有报告**，所以 `run_id` 是本次尝试的 `run_xxx`。它怎么被找到？
   `GET /v1/investigations/runs?candidate_id=...&status=failed`——"这个候选老是失败"
   正是最典型的排查入口。

## 2. 怎么调出来

| 端点 | 用途 |
|---|---|
| `GET /v1/investigations/{investigation_id}/log` | **同步**调查的日志（技术员手上是报告 id） |
| `GET /v1/investigations/jobs/{job_id}/log` | **异步**调查的日志（执行者才知道 job） |
| `GET /v1/investigations/runs?candidate_id=&status=` | 列表（新的在前）——**只回摘要**，正文按上面两个端点取 |

```bash
# 1) 先把"最近失败的运行"列出来（同步失败没有报告 id，从这里发现 run_id）
curl -H "X-Tenant-Id: tenant-a" \
  "http://127.0.0.1:8000/v1/investigations/runs?status=failed&limit=5"

# 2) 取那一次的完整过程
curl -H "X-Tenant-Id: tenant-a" \
  "http://127.0.0.1:8000/v1/investigations/run_1a2b3c4d5e6f7788/log"
```

返回体里有什么（`log` 字段）：

- `steps[]`：每步 `{step, name, args, result}`——**ReAct 轨迹**，也就是"它查了什么"。
  成功且已判定的那次只落**骨架**（`{step, name, _digest: true}`），此时 `steps_truncated=true`
  （见 §4 的写放大取舍）；失败/取消/`needs_human_review` 是全文；
- `usage` / `duration_ms` / `model`：token、耗时、用的哪个模型（"慢在哪/贵在哪"）；
- `status` / `error_code` / `error_detail`：失败上下文（**脱敏后**的 provider 原文）；
- `candidate_id` / `report_id` / `created_at`：串回业务对象。

同步调查的**响应体**里也直接带 `run_id`（= `investigation_id`），前端拿到报告的同时就知道
日志去哪取。

## 3. 告警通知里也带引用

值班人多数是从**告警**（webhook / 邮件 / IM）知道出事的，所以通知 payload 里有：

```json
{
  "event_id": "alert_...", "severity": "critical", "component_ref": "...",
  "investigation_id": "inv_...",
  "run_log_path": "/v1/investigations/inv_.../log",
  "run_log_url": "https://dog.example.com/v1/investigations/inv_.../log"
}
```

`run_log_url` 只在配了 `DD_PUBLIC_BASE_URL` 时给（**不猜部署、不把内网地址写进外发消息**）；
没配就给相对路径，接收方自己拼。没有 `investigation_id` 的老台账**不会编一个假链接**。

## 4. 设计取舍与边界（照实说）

- **脱敏 + 裁剪，不搬运证据**：`steps` 落库前过 `core/redact`（密钥/Bearer/base64 一律打码），
  单步序列化上限 4 KB、最多 40 步、失败上下文 1 KB。超长时**如实标注截断**，不假装完整。
  要看完整证据请走 `/v1/candidates/{id}/evidence`——运行日志是"过程记录"，不是证据包。
- **写放大有取舍（B3）**：一次调查最多 40 步、单步 4 KB，重跑多的候选会把库撑大。故
  `DD_RUN_LOG_FULL_STEPS`（默认 `auto`）：**成功且已判定**只落轨迹**骨架**（步号 + 工具名，
  体积≈0），**失败 / 取消 / 结论 `needs_human_review`** 落全文——排查价值恰好集中在这三类。
  两种"不完整"（骨架、40 步上限）都用 `steps_truncated` 如实标注，绝不假装完整。
  要一律留全文设 `always`；极省空间设 `never`（失败排查也会缺入参/结果）。
- **落库失败不毁结果**：同步路径写日志失败只记一条 warning（调查结论照常返回并落库）；
  异步路径日志**与终态同事务**，所以要成一起成、要不成一起不成。
- **保留期默认 90 天**（`DD_RETENTION_RUN_LOG_DAYS`，设 0 = 永久）。为什么不跟 job 的 7 天：
  日志**删了不能复原**（重跑一次得到的轨迹与当时不同），而 90 天覆盖常见事故复盘窗口；
  为什么不默认永久：它是**派生**的执行记录，结论已落在 `reports`/`alerts`。
- **不是分布式追踪**：`trace_id` 只做 correlation-id 传播（`core/tracing.py`），没有 OTel SDK，
  也没有跨服务 span 树；跨服务排查仍需外挂采集。
- **列表端点的 `total` 是全表计数**（与 `/v1/investigations/jobs` 同口径），带
  `candidate_id`/`status` 过滤时 `has_more` 只作参考。

## 5. 回归防线

`tests/test_run_log.py` 钉住这些：同步成功可回看且**带步骤**、同步失败**也留痕**（含
`error_detail`）、异步按 `job_id` 且**不再丢 trace**、列表不带正文、跨租户 404、通知载荷带
run 引用（没 `investigation_id` 时不编假链接），以及**写放大策略**（成功只落骨架 /
失败与 `needs_human_review` 留全文 / `always`/`never` 覆盖 / 非法值回落 `auto` /
API 用 `steps_truncated` 如实标注）。

```bash
pytest tests/test_run_log.py -q
```
