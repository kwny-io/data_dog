# P4-8 软实时性能基线

> 本文件记录**实测**数字。**未跑过的不填**（见 `docs/p4/README.md` 诚实边界）。
> 复现：`make bench`（= `bash tools/bench.sh`，用本机 SQLite + 离线 provider 起服务）。

## 实测结果

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

**结论**：SLO ≤500ms 在**低负载下已达标**；**中高负载下的 P99 超标源于同步落库的
排队，需要落库优化**（批量化 / 连接池 / 异步写），不是换 DB 就能解决。这是下一步。

## 下一步（按优先级）

- [ ] **写入路径批量化落库**：把单请求内的多次 INSERT 合并、或引入写队列批量落库，
      压中负载尾部延迟（当前是 SLO 未达标的主因）
- [ ] 调连接池大小，观察 P99 是否随池大小改善（区分"池不足"与"锁争用"）
- [ ] 若要对外宣称 SLO 达标，需在**目标生产环境 + 真实负载**下复测低/中/高三档

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
