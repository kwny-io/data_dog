# 写路径性能：实测报告 + 批量化方案（T1 已实施）

> 本文数字**全部本机实测**。§1 是基线复跑，§2 是根因（**含对本文上一版结论的推翻**），
> §3 是 T1 已实施内容，§4 是剩余方案与**重新定价**的预期，§5 是边界。
> 上一版（commit `6a87bb7`）把主因判为"连接池饱和"，**这个判断被实验推翻了**，见 §2.1。

---

## 0. 结论速览

1. **基线复跑成功**：`baseline.md` 的指纹数字复现——SQLite 写入 **36 req/s**（文档 35）、
   PG 低负载写入 **P99 116ms**（文档 133ms）。可以做增量对比。
2. **上一版的根因判断是错的**。连接池开到与并发齐平（60）后，P99 **没有改善**
   （1778 → 2146ms）。同一服务上跑并发曲线：8/15/30/60/90 workers 的吞吐是
   **78/79/78/77/76 req/s——完全不变**。这不是"池不够"，是**存在一个串行资源**。
3. **真正的约束是数据库的"事务吞吐"，而每请求的事务数高得离谱**：
   PG 日志实测 **15.1 条 SQL / 4~5 次事务 / 请求**（`state_kv` 行写 2.71 次、业务行写 ~1 次）。
   本机 PG 容器的事务上限实测 **143/s（单连接）/ 513/s（20 并发）**；
   `5 次事务 × 80 req/s ≈ 400/s` 已贴住该上限 → **天花板 ≈100 req/s**，与实测 70~90 吻合。
4. **T1 交付了两件事**：① **SQLite WAL 是确证的大改善**（写入 30 → **64 req/s**，
   P99 5281 → **1909ms**，3 次重复稳定）；② 连接池可配 + 候选索引去重（热路径少一次全局行锁写）。
   **但 PG 中负载 P99 仍是 ~1.4~2.1s，SLO（≤500ms）未达标**，且**靠调参达不到**。
5. 下一步的最高杠杆**不是**批量 upsert，而是**砍掉热路径上的共享状态事务**
   （限流 + 候选分组共 ~6.5 次事务/请求）——见 §4 的 T1.5。

---

## 1. 基线复跑（空载实测，改动前）

环境：Windows 11 / Python 3.12.6 / 24 逻辑核；PG 16 容器（`postgres:16-alpine`，端口 5544，
空 schema 起跑）；SQLite 走临时库；离线确定性 provider；**机器空载**。
参数与 `baseline.md` 一致：`DURATION=15`、写 `WRITE_RPS=300`、读 `READ_RPS=150`。

| 场景 | 吞吐（我） | P50 | P99 | 文档 | 结论 |
|---|---|---|---|---|---|
| SQLite 写入 60w | **36 req/s** | 1222ms | 8012ms | 35 / 6331ms | 指纹复现 ✅ |
| SQLite 读取 32w | 103 req/s | 304ms | 452ms | 56 / 903ms | 我方更好 |
| PG 写入 60w | 72 req/s | 757ms | **1778ms** | 89 / 1504ms | 同量级（差 18%） |
| PG 读取 32w | 131 req/s | 239ms | 392ms | 150 / 237ms | 同量级 |
| **PG 写入 4w@20rps（低负载）** | 20 req/s | 73ms | **116ms** | 20 / 133ms | 指纹复现 ✅ |
| PG 写入 15w@300rps | 73 req/s | 205ms | 290ms | — | 见 §2.1 的解读 |

一致性验收点全过：两侧计数偏差 ≤0.3%；`--assert-no-llm` 均为 `investigations_total=0`。

---

## 2. 根因（含对上一版结论的推翻）

### 2.1 ❌ 上一版说"连接池饱和"——被三个实验推翻

上一版的核心论证是"60 并发打默认池（5+10=15）⇒ 吞吐与 15 并发相同、P99 差 6.1 倍"。
**观测是真的，解释是错的**：

| 实验 | 结果 | 说明 |
|---|---|---|
| 池开到 **60**（对齐 60 workers） | 70 req/s，**P99 2146ms**（对比默认池 1778ms） | **没有改善** → 池不是约束 |
| 并发曲线（同一服务，只改 workers） | 8w→78、15w→79、30w→78、60w→77、90w→76 req/s | **吞吐恒定** → 存在串行资源 |
| 60w 压测期间 PG 活跃连接 | **17~23**（≈池上限 20） | 连接虽被占满，但**加大池无用** ⇒ 瓶颈在库不在池 |
| 数据面 CPU 成本（300 次 `process_batch`） | **0.05 ms/请求** | 不是 CPU/GIL |
| `synchronous_commit=off` | 62 vs 89 req/s（噪声内） | **不是 WAL fsync** |
| 共享状态后端 A/B（同库同负载） | postgres 38/31 req/s vs **memory 47/42** | 状态层占 ~25~35% |

"6.1 倍 P99 差"的正确解释是**排队长度**：固定服务率 μ≈73/s 下，
并发 15 → W≈205ms、并发 60 → W≈757ms（Little 定律 `L=λW` 两边都对得上），
与池容量无关。**在 15 workers 下负载发生器本身提供不了足够并发去暴露排队。**

### 2.2 ✅ 真正的约束：每请求 ~5 次数据库事务

用 PG 自己的日志与统计量实测（单请求顺序发、`log_statement=all`、`pg_stat_database`）：

| 口径 | 每请求 |
|---|---|
| SQL 语句总数 | **15.1** |
| BEGIN / COMMIT / ROLLBACK | **5.0 / 4.1 / 1.0** |
| `state_kv` 行写入 | **2.71** |
| `telemetry_events` + `candidates` 行写入 | ~0.95 |
| 事务数（`xact_commit` 差分） | **8.6~9.1** |
| 同上，但共享状态换 memory（对照） | **2.1** ⇒ **状态层占 ~6.5 次事务/请求** |

本机 PG 容器的事务吞吐上限：**143 commit/s（单连接）**、**513 commit/s（20 并发）**。
`5 次事务 × 80 req/s = 400 txns/s` —— 已贴住上限，**这就是 ~70~90 req/s 天花板的来源**。

热路径上的状态事务来自三处（`state/pg.py::mutate` = 一次 `engine.begin()` + `SELECT … FOR UPDATE`
+ `UPDATE` + `COMMIT`）：

1. **限流**：`rate:{tenant}|{device}`（每次请求 1 次）
2. **候选分组**：`cand:{tenant}|{device}|{component}|{signal}`（每次命中 1 次；多规则命中则多次）
3. **候选索引**：`candidate:index`（**全局单 key**；去重前每次命中 1 次 → 见 §3.2）

业务写入本身只贡献 2.1 次事务（`session_scope` + `_upsert` 的 get/SAVEPOINT/INSERT/RELEASE）。
**所以"先做批量 upsert"是错误的优先级**——它只能动 2.1 那一部分。

---

## 3. T1 已实施（本轮）

### 3.1 ✅ SQLite：WAL + `synchronous=NORMAL` + `busy_timeout`（**确证有效**）

`storage/db.py::_apply_sqlite_pragmas`，在连接事件上执行；默认开，`DD_SQLITE_WAL=0` 可关。
`engine_kwargs` 的 SQLite 分支**逐字节不变**（有精确断言守着本地行为）。

| 场景（60w@300rps，3 次重复） | 吞吐 | P50 | P99 |
|---|---|---|---|
| `DD_SQLITE_WAL=0`（旧行为） | **30 req/s**（24/30/31） | 1554ms | **5281ms** |
| 默认（WAL 开） | **64 req/s**（61/64/64） | 884ms | **1909ms** |

默认 journal + `synchronous=FULL` 是"每次 COMMIT 一次 fsync"，且写阻塞读；WAL 同时解掉两者。
**这是本轮唯一一个幅度稳定、可复现的改善。**

### 3.2 ✅ 候选索引去重（热路径少一次全局行锁写）

`candidate_shared.py::_register_index` 原本**每次命中**都 `mutate(_INDEX_KEY, …)`——而索引是
**一个全局 key**，在 PG/Redis 后端下就是**全设备、全租户共用一把行锁 + 每次一次提交**。
而它对热路径**毫无收益**：HTTP 走 `build(only_touched=True)`，索引只服务
`build(only_touched=False)` 的运维全量枚举。

改为实例内去重（FIFO 有界 4096；淘汰只导致下次多写一次，不影响正确性）。
单测锁定：同设备 20 次命中 → 索引只写 **1** 次（原来 20 次），且全量枚举完整性不变。

> **诚实说明**：这一项在**单元层面**确证（写入次数 20→1），但在本机**聚合吞吐上没有可测改善**
> （70 vs 72 req/s，噪声内）。它是一次正确的工程修复（去掉热路径上的全局锁），**不是**性能解药。

### 3.3 ✅ 连接池可配（口径修正）

`DD_DB_POOL_SIZE` / `DD_DB_MAX_OVERFLOW`（默认 10+10=20），在 `engine_kwargs` 里接上。
**保留它的理由不是"能治中负载"，而是**：容量必须显式、可审计（库默认 5+10 是隐式的），
且多副本必须满足 `(pool_size+max_overflow) × 副本数 < PG max_connections`。
上一版把它当作主因的表述已在 §2.1 更正。

---

## 4. 剩余方案（按新证据重新定价）

### T1.5 砍热路径上的状态事务（**新发现，最高杠杆**）

目标：`8.6 → ~3` 次事务/请求。若 DB 事务吞吐确为约束，吞吐应随之 **2~3×**（需实测验证）。

- **限流移出热路径的 DB**：限流桶本就是"允许少量误差"的组件。可选：
  `DD_RATE_BACKEND=redis`（已有开关，走内存级 Redis）、或 `memory`（单副本）、
  或**与候选分组合并到同一次 mutate**（同一事务里读改写两个 key → 省一次提交）。
- **索引写彻底离开热路径**：既然 `build(only_touched=False)` 是运维路径，可改为
  **按前缀扫描**（`StateStore.keys(prefix)` 已存在）枚举，`_INDEX_KEY` 可废弃（保留兼容读）。
- **合并状态事务**：`rate:` 与 `cand:` 两次独立 `engine.begin()` → 一次事务内完成（需给
  `StateStore` 加"批量 mutate"原语，或让候选分组显式携带限流判定）。

### T2 批量 upsert（原方案，**降级为第二优先**）

只影响业务侧的 2.1 次事务：`insert(...).on_conflict_do_update(...)` 单条批量，
语句数从"每行 4 条"降到"每批 1~2 条"；`_upsert` 的 SAVEPOINT/IntegrityError 分支可删。
**预期：+15~25% 吞吐**（而非上一版写的 2~3×，因为状态层才是大头）。

### T3 写队列（仍然是大改，且现在更清楚它的定位）

把**业务落库**移出请求路径；但请注意 §2.2：业务只占 2.1/8.6 次事务，
**先做 T1.5 再评估 T3**，否则会把复杂度花在小头上。语义代价（207 语义、丢数据、背压）同上一版。

---

## 5. 边界与复现

- **SLO 现状：未达标**。热路径写入 P99 在 60 并发下 **1.4~2.1s**（目标 ≤500ms）；
  低负载 P99 116ms 达标。**T1 改变不了这件事**，需要 §4 的结构性改动。
- **本机 PG 数字波动 ±25%**（相同配置重复测得 61/70/75/89 req/s）。因此
  **PG 侧 <25% 的差异一律视为噪声**；SQLite 侧差异大且稳定（24/30/31 vs 61/64/64），可信。
- **本机 ≠ 生产**：容器盘 IO、机型、负载发生器都影响结论；`synchronous_commit` 等
  生产可调项需在目标环境复测。
- 复现（PG 容器 + loadgen）：
  ```bash
  docker run -d --name dd_bench_pg -e POSTGRES_USER=ddbench -e POSTGRES_PASSWORD=ddbench_pw \
    -e POSTGRES_DB=ddbench -p 5544:5432 postgres:16-alpine
  # 起服务（DD_DATABASE_URL 指向上面那个库，DD_SERVICE_PORT=8812）后：
  python tools/loadgen.py --url http://127.0.0.1:8812 --mode write \
    --devices 60 --rps 300 --duration 15 --tenant bench-t1 --verify-server --assert-no-llm
  ```
- 每请求事务数/语句数的量法（可复现）：
  `ALTER DATABASE ddbench SET log_statement='all'` → 跑少量顺序请求 → 对 PG 日志做 tail-diff
  数 `BEGIN/COMMIT/ROLLBACK`；行写入数读 `pg_stat_user_tables`；事务总数读 `pg_stat_database.xact_commit`。
