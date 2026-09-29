# G1 调查端点异步化

## 1. 目标

消除**端到端水平扩展的最后一个瓶颈**：`POST /v1/investigations` 现在**同步阻塞**整个
请求生命周期（真实 LLM 可达 10–60s），占满 worker，无法水平扩展。改为
**提交→返回 job→轮询状态/结果**，让 worker 立即释放。

## 2. 现状（已核实）

| 已有 | 位置 | 说明 |
|---|---|---|
| 同步实现 | `service/app.py:719-782` | 同请求内 `repo.get_candidate` → `_build_investigation_provider()` → `AgentLoop(...).investigate()` → `repo.save_report`。**无** job/queue/BackgroundTasks |
| 墙钟预算 | `app.py:1062-1081` `_DeadlineProvider` | 整次调查预算 `DD_INVESTIGATE_TIMEOUT_S`(默认 90s)，到点抛 `LLMCallError(TIMEOUT)` → 504/503 |
| provider 选择 | `app.py:1084-1108` | `DD_INVESTIGATE_OFFLINE=1` → `DeterministicEvidenceProvider`（CI 离线）；否则读 key |
| 报告存储 | `storage/models.py:78-98` | `reports` 表 PK `(tenant_id, investigation_id)`；`save_report` 幂等 upsert |
| 读端点 | `app.py:705-717` | `GET /v1/investigations/{id}` 纯读库 |

**缺**：任务态表（pending/running/succeeded/failed）、job id、进度查询、取消。

## 3. 设计

### 3.1 三态：job 与 report 分离

- **job**：一次调查的**执行状态**（pending/running/succeeded/failed/cancelled），短期。
- **report**：调查的**产物**（现有 `reports` 表），长期。
- 提交返回 `job_id`；job 完成后 `report_id` 指向落库报告。

### 3.2 表

```sql
CREATE TABLE investigation_job (
    tenant_id   TEXT NOT NULL,
    job_id      TEXT NOT NULL,          -- job_<hash(tenant,candidate,ts_nonce)>
    candidate_id TEXT NOT NULL,
    status      TEXT NOT NULL,          -- pending/running/succeeded/failed/cancelled
    report_id   TEXT,                   -- 完成后填 investigation_id
    error_code  TEXT,                   -- 失败时的有限错误码
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    PRIMARY KEY (tenant_id, job_id)
);
```

### 3.3 执行：worker 池 + 租约（不引入 Celery）

**默认实现复用已有 StateStore 的原子原语**，避免新增重依赖：

- 提交：写 `pending` job → 立即返回 `202 {job_id, status:"pending"}`。
- worker：**进程内后台线程池**（`ThreadPoolExecutor`，`DD_INVESTIGATE_WORKERS`，默认 4）；
  worker 从 job 表**按租约领取**（`mutate` 把 `pending`→`running` 并记 `worker_id`+`lease_until`，
  原子保证不重复领取）。
- **多副本安全**：租约存共享状态（`state_kv` / PG），故**多副本各自跑 worker 也不会重复调查**
  ——这是"水平扩展"的关键（对齐 P4-5 的共享状态思路）。
- 完成：写 report（复用 `save_report`）→ 更新 job `succeeded`+`report_id`。
- 失败：有限错误码（沿用现有错误语义）→ job `failed`。
- **超时**：`_DeadlineProvider` 原样复用（仍给整次调查预算）；到点 → job `failed(TIMEOUT)`。
- **取消**：`POST /v1/investigations/{job_id}/cancel` → 置 `cancelled`；worker 在**步边界**检查
  （复用 deadline 机制，不强制杀线程）。

### 3.4 API（新增 + 兼容旧的同步语义）

```
POST /v1/investigations            提交 → 202 {job_id, status}.  (旧行为改为异步)
GET  /v1/investigations/jobs/{job_id}   查 job 状态（pending/running/succeeded/failed）
POST /v1/investigations/jobs/{job_id}/cancel  取消
GET  /v1/investigations/{id}       查报告（**保持现状**，id 既可是 report_id）
```

**兼容策略**（关键，避免破坏前端）：
- 加查询参数或头 `Prefer: respond-async` 控制；**默认** 202 异步。
- 或提供 `?sync=1` 走原同步路径（保留给测试与单次调试）。
- **前端须按"长任务"改写**（规格文档已提），本项提供异步语义。

### 3.5 就绪/指标

- `/readyz` 增加：异步 worker 池存活检查。
- 指标：`data_dog_investigation_jobs_total{status}`、`..._queue_depth`（pending 数）、
  `..._duration_seconds`（直方图，与 P4-8 共用）。

## 4. 要建/要改的文件

| 文件 | 动作 |
|---|---|
| `src/data_dog/service/investigation_jobs.py` | **新建**：job 生命周期 + 租约领取 + worker 池 |
| `src/data_dog/storage/models.py` | 加 `investigation_job` 表 |
| `alembic/versions/20260930_0900_g1_investigation_job.py` | **新建迁移**（含 RLS） |
| `src/data_dog/storage/repository.py` | 加 job CRUD + `claim_pending`（原子领取） |
| `src/data_dog/service/app.py` | 改 `POST /v1/investigations`；加 job 查询/取消端点；lifespan 挂 worker 池 |
| `src/data_dog/settings.py` | 加 `DD_INVESTIGATE_WORKERS`、`DD_INVESTIGATE_LEASE_S` |
| `src/data_dog/core/metrics.py` | 加 job 指标 |
| `tests/test_async_investigation.py` | **新建** |

## 5. 验收

1. **提交即返回**：`POST` 在 < 100ms 内返回 202 + job_id（不阻塞等调查完）。
2. **状态流转**：job `pending→running→succeeded`；`report_id` 可读。
3. **失败**：无 key / 超时 → job `failed` + 有限错误码；服务不 5xx 崩溃。
4. **多副本不重复**：两个 worker 副本同时抢同一 pending job → **只有一个**执行（租约原子性）。
5. **取消**：cancel 后 job 置 `cancelled`，不再写 report。
6. **水平扩展**：N 个副本 + M 个 pending job → 分布执行（断言无重复 report）。
7. **热路径不受影响**：`/v1/telemetry` 仍不触发调查、延迟不变。
8. **零回归**：旧同步路径（若保留 `?sync=1`）与现有 `test_service*` 全绿。

## 6. 诚实边界

- **进程内线程池**：worker 池是进程内的；多副本靠**共享租约**保证不重复，但**不是**分布式
  调度器。要更强保证需外部队列（Celery/RQ/Kafka），本项以"够用 + 零新依赖"为先。
- **取消是协作式**：不强制杀线程，长 LLM 调用的取消要等到该步返回（`_DeadlineProvider` 同语义）。
- **job 表会增长**：需保留策略（定期清 `succeeded` 超过 N 天的 job）——记入运维文档。
- **调查本身仍同步调用 LLM**：异步化只是让**HTTP 请求**不阻塞，单次调查的墙钟预算不变。

## 7. 依赖

无强依赖。**本项是当前缺口批里优先级最高的一项**（唯一挡着端到端水平扩展的点）。
