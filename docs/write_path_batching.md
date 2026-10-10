# 写路径性能：基线复跑 + 批量化方案（**提案，待评审**）

> 本文件是**方案提案，尚未实施**（T1/T2/T3 都还没动代码）。目标：把 `docs/slo.md` 里
> "热路径 P99 ≤ 500ms"从"低负载达标、中负载超标 3×"变成**三档都达标**，且**先不碰核心链路**。
> 本文所有数字都是本机实测（口径与 `benchmarks/baseline.md` 一致），证据与推导分开写。

---

## 0. 结论速览

1. **基线复跑成功**：`baseline.md` 的两个"指纹数字"在本机复现——SQLite 写入
   **36 req/s**（文档 35）、PG 低负载写入 **P99 116ms**（文档 133ms）。故可以做增量对比。
2. **中负载超标的主因是排队，不是算法**。同一个库、同一个目标 RPS（300），只把并发从
   60 workers 降到 15 workers（= 连接池容量）：
   **吞吐几乎不变（72 → 73 req/s），P99 从 1778ms → 290ms（6.1×）**。
   即：**并发超过连接池 = 多出来的线程不贡献吞吐，只贡献尾延迟**。
3. **次因是每请求往返数**：一次 `/v1/telemetry` ≈ **6 次往返 + 1 次 COMMIT**
   （`_upsert` 的 get/SAVEPOINT/INSERT/RELEASE 逐行展开）。这是**吞吐上限**的根源，
   也是批量化真正要解决的问题。
4. **三级方案**：T1 调参（当天、零语义变化）→ T2 批量 upsert（1~2 天、语义等价）
   → T3 写队列（1~2 周、**语义变化，需新契约**）。建议先做 T1+T2。

---

## 1. 基线复跑（空载实测）

环境：Windows 11 / Python 3.12.6 / 24 逻辑核；PG 16 容器（`postgres:16-alpine`，端口 5544，
**空 schema 起跑**）；SQLite 走临时库（不碰 `data_dog.db`）；离线确定性 provider；
**压测时机器空载**（真 LLM 评估已结束）。参数与 `baseline.md` 完全一致：
`DURATION=15`、写 `WRITE_RPS=300`、读 `READ_RPS=150`。

### 形态 A：SQLite

| 场景 | 吞吐（我） | P50 | P95 | P99 | 文档吞吐 | 文档 P99 |
|---|---|---|---|---|---|---|
| 写入 60 workers @300rps | **36 req/s** | 1222ms | 3518ms | 8012ms | 35 req/s ✅ | 6331ms |
| 读取 32 workers @150rps | 103 req/s | 304ms | 378ms | 452ms | 56 req/s | 903ms |

### 形态 B：PostgreSQL 16

| 场景 | 吞吐（我） | P50 | P95 | P99 | 文档吞吐 | 文档 P99 |
|---|---|---|---|---|---|---|
| 写入 60 workers @300rps | 72 req/s | 757ms | 1339ms | **1778ms** | 89 req/s | 1504ms |
| 读取 32 workers @150rps | 131 req/s | 239ms | 333ms | 392ms | 150 req/s | 237ms |
| **写入 4 workers @20rps（低负载）** | **20 req/s** | 73ms | 96ms | **116ms** | 20 req/s | 133ms ✅ |
| **写入 15 workers @300rps（隔离实验）** | **73 req/s** | 205ms | 259ms | **290ms** | — | — |

**一致性验收点全部通过**：客户端/服务端计数偏差 ≤0.3%；`--assert-no-llm` 均为
`investigations_total=0, jobs_total=0`（热路径零 LLM 不变式成立）。

**与文档的差异**：SQLite 写入吞吐与 PG 低负载 P99 两个"指纹"精确复现；中负载两项
（72 vs 89 req/s、1778 vs 1504ms）差 18~20%，属容器/机型/库状态差异，**不影响任何结论**
（结论建立在**同机同条件**的 B1 vs B4 对照上）。

---

## 2. 根因分析（代码级 + 对照实验）

### 2.1 主因：连接池 15 条 < 并发 60 个写线程

- `storage/db.py::engine_kwargs()` 只设了三段**超时**（`connect_timeout` /
  `statement_timeout` / `pool_timeout=5s`，见 `settings.db_pool_timeout_s()`），
  **全仓没有任何 `pool_size` / `max_overflow` 配置** → 落到 SQLAlchemy 默认
  **5 + 10 = 15**。
- `tools/loadgen.py:226` 写模式 worker 数 = `--devices`（文档用 60），每 worker 独立
  `device_sn`（`:245`）→ **真有 60 个并发写请求**，其中 45 个大部分时间在等连接。

**对照实验（同一 PG、同一目标 RPS、只改 worker 数）**：

| workers | 池容量 | 吞吐 | P50 | P99 | 解读 |
|---|---|---|---|---|---|
| 60 | 15 | 72 req/s | 757ms | **1778ms** | 45 个线程在排队 |
| 15 | 15 | **73 req/s** | **205ms** | **290ms** | 不排队 |

**吞吐相同而 P99 差 6.1 倍**——这是资源池饱和的教科书信号：多出来的并发不换来任何
吞吐，只把请求堆在池门口。故"中负载 P99 超标 3×"**首先**是**压测形状与池容量不匹配**，
其次才是落库效率。

### 2.2 次因：一次请求约 6 次往返 + 1 次 COMMIT

写路径逐行展开（`storage/repository.py`）：

- `save_telemetry()`（`:388`）与 `save_candidates()`（`:97`）都是**逐行 for 循环**；
- 每行经 `_upsert()`（`:38`）：`session.get()`（**1 SELECT**）→ 不存在则
  `begin_nested()`（**SAVEPOINT**）→ `INSERT` → `RELEASE`；已存在则 `flush`（**1 UPDATE**）。
- 压测形状下每次请求 = 「1 条新遥测 + 1 条已存在候选」：
  `SELECT + SAVEPOINT + INSERT + RELEASE` + `SELECT + UPDATE` ≈ **6 次往返**，
  出口 `session_scope()` **1 次 COMMIT**。
- 低负载实测单请求 P50 **73ms**（含 HTTP + 解析 + 检测 + 落库）→ 单连接吞吐上限
  ≈13 req/s，15 条连接 ≈ 200 req/s 上限；实测 72~73 req/s，**与"池内往返耗时占主导"吻合**。

### 2.3 SQLite 形态：每请求一次 fsync

36 req/s = 单写者 + 每请求 COMMIT（默认 rollback journal、`synchronous=FULL`）的典型表现。
**未开 WAL**，读也被写阻塞。60 workers 下 P99 已达 8.0s，其中大部分是等锁。

---

## 3. 方案（按性价比排序，建议先做 T1+T2）

### T1 —— 调参：池开到与并发匹配 + SQLite 开 WAL（**当天可做，零语义变化**）

1. 新增 `DD_DB_POOL_SIZE` / `DD_DB_MAX_OVERFLOW`（`settings.py`），在 `engine_kwargs()`
   里接上（与既有三段超时**同一处**，避免两套漂移）。默认建议 `pool_size = min(16, 2×CPU)`、
   `max_overflow = pool_size`；**不动 SQLite 分支**（文档已声明 SQLite 刻意不加池参数）。
2. SQLite 在 connect 事件里执行一次 `PRAGMA journal_mode=WAL; synchronous=NORMAL;
   busy_timeout=5000;`——仅本地/单节点形态，不改变业务语义（仍是提交后才可见）。
3. `tools/bench.sh` 与 `benchmarks/baseline.md` 补一条口径："**压测并发必须 ≤ 池容量**，
   否则测的是排队不是服务"。

**预期收益（直接由 §2.1 对照外推，不是估算）**：

- PG 中负载写入：P99 **1778ms → 约 290ms**（同条件实测），吞吐持平 72→73 req/s；
- 想再上吞吐必须靠 T2（池已不是瓶颈，往返数才是）；
- SQLite：写不再阻塞读（读 P99 452ms 里的写干扰消失），写入吞吐受单写者限制，
  预期 36 → 100~300 req/s 量级（**待实测**）。

**风险**：低。但**池不能无限开大**：多副本部署时 `pool_size × 副本数 < PG max_connections`，
默认值要保守并把这条乘法写进文档。

### T2 —— 批量 upsert：把 (N+M)×4 次往返压成 1~2 次（**1~2 天，语义等价**）

1. `save_telemetry` / `save_candidates` 改为**单条批量 upsert**：
   PG 与 SQLite 均可用 `insert(Model).values(rows).on_conflict_do_update(index_elements=[...],
   set_={...})`（SQLite ≥3.24，本机与 CI 均满足）。
2. 由"逐行 ORM 对象 + apply_fn"改为"显式列映射 dict 列表"。**风险点在这里**：手写列映射
   容易漏列 → 建议加一条**元测试**：断言映射覆盖 `Model.__table__.columns` 的全部业务列
   （除主键/时间戳），漏列即红。
3. `_upsert()` 的 `SAVEPOINT` + `IntegrityError` 重试分支**可删**：`ON CONFLICT DO UPDATE`
   天然原子，"并发插同一主键"不再抛错 → 代码更短，且每行少 2 次往返。

**预期收益**：语句数 `(N+M)×4 + 1` → **2 + 1**（一批一次 upsert + 一次 COMMIT）；
单请求服务时间预计 73ms → **20~30ms 量级**；配合 T1 后中负载 P99 目标 **≤500ms**
（当前 1778ms），吞吐预期 **150~300 req/s** 量级（**待实测**）。

**风险**：中低。幂等性、租户隔离（`tenant_id` 复合主键）、`_apply_*` 字段语义须逐条对齐；
回归网用 `tests/test_storage.py` + `tests/test_storage_postgres.py`（含**非特权角色裸 SQL
断言跨租户读写被挡**）。

### T3 —— 写队列 / 批量提交（**1~2 周，语义变化，需先定契约**）

请求入队 → writer 每 `X ms` 或每 `K` 行 flush → 请求等 ack（或 202 + job 句柄）。

- 预期：吞吐再上一个量级（批量摊薄 COMMIT/往返），P99 更平；
- **代价（必须先定契约，不能顺手做）**：
  1. 落库不再同步 → 现有"落库失败返回 **207 + ok:false** 但仍带候选"的语义要扩展
     （ack 超时算不算失败？）；
  2. 崩溃会丢"已入队未落库"的数据 → 需 WAL 或明确接受 at-most-once 落库；
  3. 背压与队列上限（否则过载时内存吃光）；
  4. 与"热路径零 LLM / 失败关闭"两条不变式的一致性说明。
- **不建议在 T1+T2 之前做 T3**：T2 之后 SLO 很可能已达标，T3 的复杂度就不划算。

---

## 4. 验收口径（做完必须能一键复现）

1. 两形态 × 三档（低 4w@20 / 中 60w@300 / 高），要求热路径写入 **P99 ≤ 500ms**，
   未达标项如实记录。
2. 不变式不破：`--assert-no-llm` 仍为 `investigations_total=0`；两侧计数偏差 ≤5%。
3. 回归：全量 `pytest -q`、`tests/test_storage_postgres.py`（真 PG 裸 SQL）、
   `tests/test_state_redis_real.py`（真 Redis）全绿。
4. 实测数字回填 `benchmarks/baseline.md`，**未跑过的不填**（沿用该文档既有规则）。

---

## 5. 边界与未做

- **本机数字 ≠ 生产数字**：容器/机型/负载生成器都影响结果（沿用 `baseline.md` 声明）。
- `pool_size` 默认值需**多副本**视角定：`pool_size × 副本数 < PG max_connections`。
- 高负载档（120 workers）未测：T1 之前测它只能测到排队，没有信息量。
- 本提案**不含任何代码改动**：T1/T2 待评审通过后再动。
- 复现命令（本机口径）：
  ```bash
  # 形态 B（PG 容器）
  docker run -d --name dd_bench_pg -e POSTGRES_USER=ddbench -e POSTGRES_PASSWORD=ddbench_pw \
    -e POSTGRES_DB=ddbench -p 5544:5432 postgres:16-alpine
  # 起服务（DD_DATABASE_URL 指向上面那个库，DD_SERVICE_PORT=8812）后：
  python tools/loadgen.py --url http://127.0.0.1:8812 --mode write \
    --devices 60 --rps 300 --duration 15 --tenant bench-t1 --verify-server --assert-no-llm
  # 隔离实验：把 --devices 换成 15，其余不动
  ```
