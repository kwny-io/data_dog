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
---

## 8. 热表按月分区（架构评审 A9）

`telemetry_events` 是唯一的**只增**大表，保留期一到靠 `delete_in_batches` 一批 500 行地删。
分区把"删数据"变成"删表"：`DROP TABLE` 是 O(1)、不产生死元组、不需要 VACUUM，也不让"最近 7 天"
的查询跟整表历史争索引。**默认不开启**——它是需要一次结构变更的部署选择，不是代码开关。

### 8.1 日常维护（已分区之后）

```bash
python scripts/partition_telemetry.py --check                  # 只读：是否分区 / 有哪些分区
python scripts/partition_telemetry.py --ensure-months 3        # 建"本月起未来 3 个月"分区（幂等）
python scripts/partition_telemetry.py --drop-before 2026-08    # 删"整段早于 2026-08"的分区
```

- `--ensure-months` 建议挂**每月定时任务**：分区表最容易踩的坑就是"忘了建下月分区 → 写入直接
  报 `no partition of relation ... found for row`"。
- `--drop-before` 只删**整段过期**的分区（上界 ≤ 截止时间）；边界月里可能还有要留的数据，
  宁可少删（保数据），留给下一轮。
- 一旦确认是分区表，`retention` 的 `telemetry_events` 规则会**自动**改走 `DROP PARTITION`
  （报告里 `mode=drop_partition` + `dropped_partitions`，`total` 只统计逐行删除，不混口径）。
  非 PG / 未分区 → 原分批 `DELETE`，行为完全不变。

### 8.2 为什么 `telemetry_events` **现在转不了**（真 PG 实测）

PG 有一条硬规则：**分区表的唯一约束必须包含全部分区键**。实测报错原文：

```
FeatureNotSupported: unique constraint on partitioned table must include all partitioning columns
```

而 `telemetry_events` 的主键是 `(tenant_id, event_id)`——它正是写入路径
`INSERT ... ON CONFLICT DO UPDATE` 的**去重键**（`repository.save_telemetry`）。于是：

| 方案 | 能否保留去重键 | 能否按时间 `DROP PARTITION` | 结论 |
|---|---|---|---|
| `RANGE (created_at)` + 原主键 | — | — | **PG 直接拒绝**（唯一约束不含分区键） |
| `RANGE (created_at)` + 主键加 `created_at` | ❌ 同 `event_id` 不同 `created_at` 会重复入库 | ✅ | 拿**数据完整性**换清理成本，需产品决定 |
| `HASH (tenant_id, event_id)` | ✅ | ❌ 各分区混着所有年代 | 不解决 A9 要省的那笔 DELETE |

所以这不是"实现没做完"，而是**"重复上报去重"与"按年龄删分区"二选一**的数据模型决策。

**推荐（2026-10-14 真库实测后的工程判断）**：**暂不转换，先盯实测触发线**；越过再走「摄取日桶」
方案，而不是上面三条里的任何一条。

| 方案 | 幂等（重投递去重） | 按时间删分区 | 判定 |
|---|---|---|---|
| A：`RANGE(created_at)` + 主键加 `created_at` | ❌ 重投递会重复入库 | ✅ | **排除**（数据完整性问题） |
| B：`HASH(tenant_id, event_id)` | ✅ | ❌ 省不下那笔 DELETE | **排除**（花了停机窗口迁移却拿不到收益） |
| C：维持现状（分批 DELETE） | ✅ | — | **默认**：已有 `(tenant_id, created_at)` 索引 + 有界批次 + 单持有者租约 |
| **D：`RANGE(created_day)`**：`created_day = floor(created_at/86400)` 由**写路径维护**的普通列，主键 `(created_day, tenant_id, event_id)` | ✅ **同日**重投递仍去重（跨天会重复 → 写进边界 + 指标） | ✅ | **已选定并开工（第 1/3 步已落地）** |

为什么 A 一定会破坏幂等（两处代码事实）：`repository._upsert_stmt` 把 `created_at` 放进 `keep_out`
（**首次**写入时间不被覆盖），而真 PG introspection 显示该列**没有 server default**——重投递时 ORM
会带一个**新的** `created_at`；它一旦进主键，`ON CONFLICT` 就不再命中，于是插入重复行。

为什么 D 不破坏保留语义：retention 按**摄取时间**删除（刻意不用事件时间，否则补传的历史事件会
"永远年轻"）；`created_day` 正是摄取时间的日粒度派生，晚到的历史事件落在**今天**的桶里，不会被误删。

**触发线（用 `tools/loadgen.py` 实测，不要用 `docs/slo.md` §2 的设计预算当现状）**：7 天窗口的

**D 的机制已在真 PG 验证**（`postgres:16-alpine`，2026-10-14）：建表（主键含分区键）成功；同日用两个
不同 `created_at` 重投递同一 `(tenant, event_id)` → **仍是 1 行**（去重保住）；跨天重投递 → 2 行（边界
如其所述）；`DROP TABLE` 昨日分区 **0.009 s**，今日数据不受影响。

**实现进度（第 1/3 步已落地）**：

- **schema + 写路径**：`TelemetryEventRow.created_day` 进主键；`models.day_of()` 是唯一取整来源；
  `save_telemetry(..., now=)` 用**同一时刻**算 `created_at`/`created_day`（不会跨午夜差一天）；
  迁移 `a9c1d2e3f4b5` 加列 → 回填 → 换主键（PG `ALTER`、SQLite batch；回填用 `floor`，不用 PG
  的 double→int 四舍五入）。
- **代价可观测**：`repository.count_cross_day_duplicates()` 找出同一 `(tenant_id, event_id)` 落在
  多个日桶的行。它**刻意不挂 `/metrics` 或 retention 热路径**（`GROUP BY` 扫描不便宜），由运维
  按需/定期跑；`since` 传保留窗口起点可在分区表上触发分区裁剪。
- **分区模块已改日粒度（第 2/3 步已落地）**：分区键 `created_day`，分区粒度仍是**自然月**
  （一个分区覆盖该月所有日桶），DDL 边界是**日序号整数**（`FOR VALUES FROM (20727) TO (20758)`）；
  `partition_end()` 仍返回 epoch 秒（20758 × 86400），因此 retention 的 `cutoff` 比较逻辑一行未改。
  真 PG 往返用例已改成日桶形态（`created_day bigint` 进主键），并新增"插入确实落进那个物理分区"
  的断言。
- **转换已脚本化并真库验证（第 3/3 步已落地）**：`partitioning.convert_to_partitioned()` +
  `scripts/partition_telemetry.py --convert [--dry-run] [--keep-old]`。单事务里：
  `CREATE TABLE new (LIKE old INCLUDING ALL) PARTITION BY RANGE (created_day)` → 按既有数据月份
  建分区 → 拷数据 → **显式复制 `LIKE` 不复制的东西**（RLS 开关、策略、非属主授权）→ 核对行数 →
  改名切换 → 删旧表。失败整体回滚。真库实测的两处坑已修：① 索引名是 schema 级唯一，**必须先删
  旧表**再改回规范索引名，否则 `relation pk_... already exists`；② 主键要改**约束**名（改索引名
  不动约束名）。

**一个必须知道的限制**：`created_day` **不能是生成列**——PG 16 明确拒绝"生成列做分区键"（实测报错
`Column "created_day" is a generated column.`），PG 17 才允许。所以 D 要有一个**普通列 + 写路径显式
赋值**（配合 upsert 的 `index_elements` 与模型/迁移改动）。这也是"要停机窗口 + 改数据模型"的具体
内容，不是一句"加个分区"能带过的。
`telemetry_events` 行数达到 **≈5,000 万（约 25 GB）**，或单轮 retention 的 DELETE 超过 **≈60 s**，
再上 D。**本机真库实测**（单节点 `postgres:16-alpine`，1,000,000 行 / 202 MB，payload 比 SLO 的
0.5 KB 偏小）：分批 DELETE（500/批）**≈52,000 行/秒** → 默认一轮（`max_batches=200`，最多 10 万行）
约 **1.9 s**；整段过期分区 `DROP TABLE` **0.03 s**（与行数无关、不产生死元组）。即：**在百万行量级
DELETE 还不是问题**——所以先定触发线，而不是先动表结构。

**已有防护**：`partitioning.check_partitionable()` 会逐条点名阻塞项（非 PG / 表不存在 / 分区键
类型不支持 / **唯一约束不含分区键** / 入向外键），`--check` 会打印，转换类操作在阻塞项存在时
直接拒绝。真库回归用例 `test_real_telemetry_shape_is_blocked_by_its_primary_key` 用同形探针把
"不行 + 被谁挡住"钉死。

### 8.3 转换 runbook（已脚本化，但仍是停机窗口操作）

```
# 0. 先看能不能转（阻塞项会逐条列出；非 PG 直接拒绝）
python scripts/partition_telemetry.py --check
# 1. 看计划：多少行、建哪些分区、按什么顺序动
python scripts/partition_telemetry.py --convert --dry-run
# 2. 停止接入（见下），然后在低峰期执行
python scripts/partition_telemetry.py --convert            # 加 --keep-old 可保留 <表>_old
# 3. 验证
python scripts/partition_telemetry.py --check              # 应为"分区表，共 N 个分区"
```

**为什么必须停机**：转换在**单事务**里完成，`INSERT INTO new SELECT * FROM old` 拿的是读快照——
事务期间的并发写入不会进新表，会在切换时丢失；`RENAME` 还需要 `ACCESS EXCLUSIVE`。失败会整体
回滚（旧表原样不动），所以"停一下 + 事务原子性"就足够，不需要手工备份表。

**脚本替你做的事**（都在同一个事务里）：建结构等价的新分区父表（`LIKE ... INCLUDING ALL` 复制
列/默认值/约束/索引）→ 按既有 `created_day` 的月份区间 + 未来 N 个月建分区 → 拷数据 → 复制
**RLS 开关、策略、非属主授权**（`LIKE` 不复制这三样，真库实测）→ 核对新旧行数（不等就回滚）→
`RENAME` 切换 → 分区与索引名改回规范名 → 删旧表（`--keep-old` 时保留为 `<表>_old`）。

**回填历史数据注意**：新写路径把 `created_day` 记为**摄取日**。若你要灌历史事件并希望它们按
事件时间分区，必须先手工建好对应月份的分区，否则会撞 `no partition of relation ... found for row`。

### 8.4 诚实边界

- **只在 PostgreSQL 上有效**；SQLite（测试/单机演示）走原路径。非 PG 上执行维护 CLI 会明确
  拒绝（退出码 2），不会假装成功。
- **分区键边界是 epoch 秒数值**（`created_at` 是 `double precision`）。上一版生成的时间戳字面量
  在真库上会报 `invalid input syntax for type double precision`——已修，并有真库用例钉住。
- **真库验证已在本机完成**：起 `postgres:16-alpine`（端口 55432）后，`-m postgres` 用例
  **24 passed**（含建分区→插入→删过期分区往返，以及"该表被主键阻塞"的回归）。
- **`telemetry_events` 的转换已实现并真库验证**（探针表 + 真表都跑通）：`--convert` 转换真表后，
  `save_telemetry` 的行确实落进**当月分区**，过期分区被整段 DROP。仍**未做**：多副本并发下跑
  迁移的协调（`DD_AUTO_MIGRATE=0` 由部署流程负责）、亿级表的分批回填（当前回填是一条 `UPDATE`）。
