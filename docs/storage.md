# 持久化层（SQLAlchemy 2.0）

> 把项目里**内存态**的核心对象落库，重启不丢——把交付档次从 L1（可复现 Demo）推到 **L2（MVP 可交付）**。
> 架构 §15「数据、状态与存储」的最小落地。

---

## 1. 一句话

**同一份代码，本地用 SQLite（零安装），生产用 PostgreSQL；改一个连接串即可，代码不动。**

```bash
pytest tests/test_storage.py -q     # 13 项
```

---

## 2. 为什么用 SQLAlchemy + PostgreSQL（而不是 Redis）

| | PostgreSQL | Redis |
|---|---|---|
| 本质 | 关系型数据库，**落盘持久** | 内存键值库，主要为**快** |
| 用途 | **长期存储 / 查询 / 审计 / 可追溯** | 缓存 / 会话 / 限流计数 |
| 本项目 | 事件、候选、报告、Alert、Outbox | （将来）网关限流、热点缓存 |

**"长期存储" = 要落盘、要活过重启 → PostgreSQL。** Redis 是配角（快但不保证持久），不适合做长期存储。
**SQLAlchemy 是工具**：让你不写 SQL、且能在 SQLite / PostgreSQL 之间无缝切换。

---

## 3. 模块

| 文件 | 职责 |
|---|---|
| `db.py` | 引擎/会话管理、`Base`、`init_db`；`DD_DATABASE_URL` 决定连接串 |
| `models.py` | ORM 表（见下表） |
| `repository.py` | 仓储层：领域对象 ↔ 行的转换、幂等 upsert、查询（**所有方法强制 `tenant_id`**） |
| `migrations.py` | alembic 迁移封装；启动走 `upgrade_head` |

### 表

| 表 | 内容 | 来源 | 主键 |
|---|---|---|---|
| `telemetry_events` | 规范化遥测事件 | P2 | `telemetry_id` |
| `candidates` | 候选异常（§12.3） | P2 | `(tenant_id, candidate_id)` |
| `reports` | 调查报告（§12.4） | Agent | `(tenant_id, investigation_id)` |
| `alerts` | 正式事件（§12.5） | P3 | `(tenant_id, event_id)` |
| `alert_outbox` | 上报 outbox（幂等，要求持久） | P3 | `(tenant_id, idempotency_key, revision)` |
| `alert_feedback` | 人工/维修反馈 | P3 | `(tenant_id, id)` |
| `knowledge_documents` | 用户导入的厂商文档元数据 | A7 | `(tenant_id, document_id)` |
| `knowledge_chunks` | 文档切片文本 | A7 | `(tenant_id, chunk_id)` |
| `audit_log` | **安全审计**：谁在何时对什么做了什么、结果如何（只增不改） | A1 | `(tenant_id, audit_id)` |
| `investigation_run_log` | **调查运行日志**：ReAct 每步轨迹 + 用量耗时 + 失败上下文（技术员事后排查；见 [run_log.md](run_log.md)） | 调查执行 | `(tenant_id, run_id)` |

**多租户**：`tenant_id` 进复合主键——确定性 ID（如 `cand_<hash(device_sn|...)>`）会让两个租户同 `device_sn` 撞同一 PK，`_upsert` 就会静默覆盖他人数据。把租户纳入主键是**正确性要求**，不是可选的过滤列。

**数据库强制层（PostgreSQL RLS）**：迁移 `a7b8c9d0e1f2` 对租户表启用 `ROW LEVEL SECURITY` + 策略（`USING`/`WITH CHECK (tenant_id = current_setting('app.tenant_id', true))`）；会话通过 `set_config('app.tenant_id', :t, true)`（事务作用域、绑定参数）注入租户。两条硬边界：**superuser / 表属主默认绕过 RLS** → 生产须用非属主、非 superuser 角色；未设 `app.tenant_id` → 谓词为 NULL → **零行**（失败关闭）。SQLite 无 RLS，本地开发靠上面的应用层隔离。真库验证见 `tests/test_storage_postgres.py`。

**系统级跨租户扫描（`session_scope(system=True)`）——顺带修掉一个静默失效**：上面那条"未设租户 → 零行"是对的失败关闭，但**后台扫描本来就没有租户**（升级 sweeper 要跨租户找到期投递、异步 job 恢复扫描要跨租户找卡死任务）。实测（非特权应用角色 + PG 16 + 本仓迁移）：同一张表，superuser 看到 1 行、应用角色**未设上下文看到 0 行**、设了租户才看到 1 行 —— 也就是说 `sweep_once()` 在生产 PG 上恒为 `scanned=0`，**告警升级根本不发生、且不报错**；CI 未发现是因为 CI 的 PG 腿连的是 superuser（绕过 RLS）。故迁移 `b7c8d9e0f1a2` 给策略加了一条**显式**通道：

```
USING (tenant_id = current_setting('app.tenant_id', true)
       OR coalesce(current_setting('app.system_scan', true), 'off') = 'on')
```

三条纪律：① 只有后台扫描传 `system=True`（全仓仅 2 处调用，可 grep；请求路径绝不传）；② 扫描只负责"找出哪条要处理"，逐条处理仍走 `session_scope(tenant_id=...)` 并照旧带该行的 `tenant_id`；③ **未设上下文依旧零行**——失败关闭不放松（事务作用域，不泄漏到下个事务）。真库回归见 `tests/test_storage_postgres.py::test_rls_system_scan_is_explicit_and_scoped` 与 `..._finds_due_escalations`。

**设计取舍**：多态字段（metrics / evidence / hypotheses / history …）用 **JSON**——**PostgreSQL 上落成 JSONB**（SQLite 存 JSON 文本），关键字段单独建列 + 索引，避免过早拆表。

> ⚠️ 这句话曾经是**假的**：baseline 迁移用 `sa.JSON()`，在 PG 上落的是 `json`（不是 jsonb）——不能建 GIN 索引、不去重、每次取值都要重新解析，而文档一直写着 JSONB。现由迁移 `f2a3b4c5d6e7` 把既有库 ALTER 成 jsonb，并由两条 meta 测试把关：`test_json_columns_are_jsonb_on_postgres`（逐列按 PG 方言编译检查**真的是 JSONB**）+
> `test_jsonb_migration_covers_every_json_column`（迁移的列清单必须与模型元数据逐条一致——新增 JSON 列忘了改迁移会红灯）。
> 同一条迁移还补了**租户维度复合索引**（`(tenant_id, created_at[, tiebreaker])` 覆盖 telemetry/candidates/reports/alerts/jobs）：这些列表都是"租户过滤 + 时间排序"，而单列索引 PG 只能二选一。

---

## 4. 用法

```python
from data_dog.storage.db import init_db, session_scope
from data_dog.storage import repository as repo

init_db()                                  # 建表（幂等）
with session_scope() as s:
    repo.save_candidates(s, candidates, tenant_id="t1")    # 幂等 upsert
    repo.upsert_outbox(s, tenant_id="t1", idempotency_key="dev:thermal:e1", revision=1, payload={...})
with session_scope() as s:                 # 新会话（模拟重启）
    print(repo.list_candidates(s, tenant_id="t1", device_sn="dev-go2-0001"))
```

---

## 5. 切换数据库（本地 ↔ 生产）

```bash
# 本地开发（默认）：SQLite 文件，零安装
#   → ./data_dog.db

# 生产：PostgreSQL（装驱动 + 设环境变量）
pip install -e ".[postgres]"
export DD_DATABASE_URL="postgresql+psycopg://user:pw@host:5432/data_dog"
```

连接串示例：
```
sqlite:///data_dog.db                                  # 本地
postgresql+psycopg://user:pw@host:5432/data_dog        # 生产
```

**代码零改动**——这是用 SQLAlchemy 的核心收益。

---

## 6. 安全

- **无字符串拼接 SQL**：全部走 ORM 的 `session.get` / `session.query` / 绑定参数；
- 幂等写：按主键 / 复合主键 upsert，重复保存不产生重复记录（`alert_outbox` 用 `(tenant_id, 幂等键, revision)` 复合主键强制去重）。

---

## 7. 与架构条目对照

| 架构 §15 存储 | 本项目 | 状态 |
|---|---|---|
| Event Log（规范化事件） | `telemetry_events` | ✅ |
| Alert Outbox（幂等、重试、死信） | `alert_outbox` | ✅ |
| Evidence / Candidate / Report | `candidates` / `reports` | ✅ |
| Control DB / Artifact Registry | — | ❌（属 P4 控制面） |
| Time-series Store | 复用 PostgreSQL（可加 TimescaleDB） | 🟡 |
| Document Store（不可变、加密） | 文件系统 + revision 哈希（P1） | 🟡 |
| State Store（Checkpoint） | — | ❌（属 P4 实时流） |

> 这一步把项目从"重启即丢"变成"**数据可持久化、可审计**"，是 L1→L2 的关键一环。
