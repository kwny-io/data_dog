# P4-8 软实时性能基线

> 本文件记录**实测**数字。**未跑过的不填**（见 `docs/p4/README.md` 诚实边界）。
> 复现：`make bench`（= `bash tools/bench.sh`，用本机 SQLite + 离线 provider 起服务）。

## 复测（2026-10-16）

**环境**：本机 Windows 11 / Python 3.12.6 / 离线确定性 provider；**本机同时还有另一个 agent 在跑
任务**，故数字应视为**下界**（比空载差）。
**日期**：2026-10-16
**命令**：`.\tools\bench.ps1 -Port 8811 -Duration 15 -Devices 60 -WriteRps 300 -ReadRps 150`。
Windows 没有 bash（本机 WSL 只有桩）时用它——步骤与 `tools/bench.sh` 一一对应：起服务 → 等
/healthz → loadgen 写/读各一轮 → 抓 /metrics。PG 形态加
`DD_DATABASE_URL=postgresql+psycopg://ddbench:ddbench_pw@127.0.0.1:5544/ddbench`（独立容器，见下）。

| 配置 | 写吞吐 | 写 P50 | 写 P95 | 写 P99 | 写 max | 写错误 | 读吞吐 | 读 P99 |
|---|---|---|---|---|---|---|---|---|
| SQLite，写队列**关**（默认） | 201 req/s | 221ms | 378ms | **2296ms** | 5836ms | 0 | 113 req/s | 769ms |
| SQLite，写队列开，队列 1000（默认） | **300 req/s** | 142ms | 199ms | **217ms** | 326ms | **970 × 503** | 111 req/s | 778ms |
| SQLite，写队列开，队列 5000 | **300 req/s** | 138ms | 199ms | **221ms** | 305ms | **0** | 115 req/s | 756ms |
| PG 16（容器），写队列关 | 109 req/s | 552ms | 639ms | **752ms** | 1119ms | 0 | 150 req/s | 383ms |
| PG 16（容器），写队列开，队列 5000 | 152 req/s | 386ms | 464ms | **507ms** | 751ms | 0 | 150 req/s | 449ms |

五种配置共同满足的验收点：**双侧一致**（客户端直方图与服务端直方图偏差 ≤0.1%）、
**热路径零 LLM**（压测期间 `investigations_total` / `jobs_total` 增量 = 0）。

**结论（可行动的）**：

1. **写队列把尾部延迟降了一个数量级**：SQLite 写 P99 2296ms → 217ms，吞吐同时 201 → 300 req/s
   （打满目标）。它不是"写得更快"，而是**把同步落库移出请求路径**。PG 形态同样改善：
   P99 752ms → 507ms（仍略高于 SLO 500ms）。
2. **默认队列 1000 在突发下会满，且这是设计如此**：60 并发 ×300rps 持续 15s，
   **970/4500 = 21.6% 被 503 拒收**（`write_behind_total{result="rejected_full"}`，失败关闭、不静默丢，
   调用方可重试）。队列调到 5000 → **0 错误、P99 221ms**：说明**写线程跟得上（≥300 rps），
   瓶颈是队列容量而不是写线程**。生产必须显式定容（`DD_WRITE_BEHIND_QUEUE`），否则突发会看到 503。
3. **PG 中负载写 P99 依旧贴着 SLO 边缘**（507ms）：低负载此前实测 133ms 达标，中负载仍在
   500ms 上下。要对外宣称达标，需在目标环境按低/中/高三档复测。
4. **与 2026-09-29 的数字不可逐项归因**：期间代码有多处改动（写队列、观测、证据、知识面），
   机器负载也不同。**只做趋势判断，不做单因素因果宣称。**

## 实测结果（2026-09-29，首次）

**环境**：本机 Windows 11 / Python 3.12.6 / 离线确定性 provider
**日期**：2026-09-29

### 形态 A：SQLite（单文件）

**命令**：`PORT=8811 DURATION=15 DEVICES=60 WRITE_RPS=300 READ_RPS=150 bash tools/bench.sh`

| 场景 | workers | 目标 RPS | 实测吞吐 | P50 | P95 | P99 | max | 错误 |
|---|---|---|---|---|---|---|---|---|
| 遥测写入 `POST /v1/telemetry` | 60 | 300 | **35 req/s** | 1346ms | 4109ms | **6331ms** | 8021ms | 0 |
| 候选读取 `GET /v1/candidates` | 32 | 150 | **56 req/s** | 570ms | 817ms | **903ms** | 1029ms | 0 |

### 形态 B：PostgreSQL 16（容器，端口 5544；`postgres:16-alpine`）

**命令**：`DD_DATABASE_URL=postgresql+psycopg://…@127.0.0.1:5544/… PORT=8812 DURATION=15 … bash tools/bench.sh`

| 场景 | workers | 目标 RPS | 实测吞吐 | P50 | P95 | P99 | max | 错误 |
|---|---|---|---|---|---|---|---|---|
| 写入（中负载） | 60 | 300 | **89 req/s** | 605ms | 1173ms | **1504ms** | 2047ms | 0 |
| 读取（中负载） | 32 | 150 | **150 req/s** | 162ms | 207ms | **237ms** | 319ms | 0 |
| **写入（低负载）** | 4 | 20 | 20 req/s | **83ms** | 111ms | **133ms** | 158ms | 0 |

**两侧一致性**（验收点 2）：三种场景偏差均 ≤0.2% ✓
**热路径零 LLM**（验收点 5）：压测期间 `investigations_total` / `jobs_total` 增量 = 0 ✓

**PG 相对 SQLite**：写入吞吐 35 → 89 req/s（2.5×），写入 P99 6.3s → 1.5s；读取 P99 0.9s → 0.24s。**换 PG 大幅改善，印证 SQLite 单写入者串行化是形态 A 的主因。**

## 与 SLO 的差距（如实记录，不粉饰）

`docs/slo.md` 声明**热路径 P99 ≤ 500ms**。PG 形态实测：

| 负载 | 写入 P99 | 对 SLO 500ms | 读取 P99 | 对 SLO 500ms |
|---|---|---|---|---|
| **低负载**（4 workers @ 20 rps） | **133ms** | ✅ 达标 | — | — |
| 中负载（60 workers @ 300 rps） | 1504ms | ❌ 超标 3× | 237ms | ✅ 达标 |

**归因（已核实，非猜测）**：

1. **单请求开销本身是达标的**：低负载 P99 133ms < 500ms → 计算链路（网关→解析→
   检测）+ 单次同步落库的 P99 在目标内。**SLO 未达标只出现在并发争用下。**
2. **中负载 P99 由排队主导**：60 并发写时，落库在 `session_scope()` 里**同步**执行，
   同一 tenant 的写串行化 + 连接池排队 → 尾部延迟被等待时间拉高。吞吐 89 req/s
   说明系统能处理，但**延迟不平稳**（P50 605ms vs P99 1504ms，尾部拖长）。
3. **形态是主因，不是算法**：SQLite 写入 P99 6.3s → PG 1.5s（4×），低负载 PG 仅
   133ms。换后端是必要改善，但**未消除中负载的排队**。

**复测后（2026-10-16）**：同样 60 并发 ×300rps，PG 写 P99 已从 1504ms 降到 **752ms**（写队列关）/
**507ms**（写队列开、容量 5000）——排队仍是主导，但已贴近 SLO 500ms；SQLite 形态开队列后
P99 217ms **进入 SLO**，代价是默认队列容量必须显式定容（否则突发 503）。即"中高负载需要落库
优化"**部分已兑现**（T1 写队列），余量见上面的矩阵。

**结论**：SLO ≤500ms 在**低负载下已达标**；**中高负载下的 P99 超标源于同步落库的
排队，需要落库优化**（批量化 / 连接池 / 异步写），不是换 DB 就能解决。这是下一步。

## 下一步（按优先级）

- [x] ~~写入路径批量化落库~~：**已实施**（T1 写队列：`service/write_behind.py`、`DD_WRITE_BEHIND`，
      **默认关闭**；设计见 `docs/write_path_batching.md`）。复测数字见上（P99 2296ms → 217ms）。
      剩下的不是"实现"，而是**定容与默认值决策**：
- [ ] **给写队列定容并把默认值说清楚**：默认 `DD_WRITE_BEHIND_QUEUE=1000` 在 60 并发 ×300rps 下
      拒收 21.6%（503）；队列 5000 → 0 错误。要么显式调大，要么把"503 是设计内的背压"写进
      SLO 与告警口径（`write_behind_total{result="rejected_full"}` 已有告警）。
- [ ] **补齐连接池实验**：调连接池大小，观察 P99 是否随之改善（区分"池不足"与"锁争用"）。
- [ ] 若要对外宣称 SLO 达标，需在**目标生产环境 + 真实负载**下复测低/中/高三档。

## 复现方式

```bash
# 起独立 PG 容器（勿用现有 5432 上的库）
docker run -d --name dd_bench_pg -e POSTGRES_USER=ddbench \
  -e POSTGRES_PASSWORD=ddbench_pw -e POSTGRES_DB=ddbench -p 5544:5432 postgres:16-alpine

# 指向 PG 跑基准
DD_DATABASE_URL="postgresql+psycopg://ddbench:ddbench_pw@127.0.0.1:5544/ddbench" \
  PORT=8812 bash tools/bench.sh
```

## 诚实边界（引用 `docs/p4/08_soft_realtime.md`）

- **硬实时 <1ms 非目标**：Python + FastAPI + 关系库往返物理上做不到，改述为软实时。
- **本机数字 ≠ 生产数字**：环境差异大，生产须在目标环境重跑。
- **单机 loadgen 是近似**：loadgen 自身可能成为瓶颈，数字是下界近似。
- 本文档**不填**未跑过的数字。
