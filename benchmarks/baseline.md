# P4-8 软实时性能基线

> 本文件记录**实测**数字。**未跑过的不填**（见 `docs/p4/README.md` 诚实边界）。
> 复现：`make bench`（= `bash tools/bench.sh`，用本机 SQLite + 离线 provider 起服务）。

## 实测结果

**环境**：本机 Windows 11 / Python 3.12.6 / SQLite（单文件）/ 离线确定性 provider
**日期**：2026-09-29
**命令**：`PORT=8811 DURATION=15 DEVICES=60 WRITE_RPS=300 READ_RPS=150 bash tools/bench.sh`

| 场景 | workers | 目标 RPS | 实测吞吐 | P50 | P95 | P99 | max | 错误 |
|---|---|---|---|---|---|---|---|---|
| 遥测写入 `POST /v1/telemetry` | 60 | 300 | **35 req/s** | 1346ms | 4109ms | **6331ms** | 8021ms | 0 |
| 候选读取 `GET /v1/candidates` | 32 | 150 | **56 req/s** | 570ms | 817ms | **903ms** | 1029ms | 0 |

**两侧一致性**（验收点 2）：写入偏差 0.2%、读取 0.1%，均 ≤5% ✓
**热路径零 LLM**（验收点 5）：压测期间 `investigations_total` / `jobs_total` 增量 = 0 ✓

## 与 SLO 的差距（如实记录，不粉饰）

`docs/slo.md` 声明**热路径 P99 ≤ 500ms**。本次实测：

- 写入 P99 **6331ms** → **超标约 12.7 倍**
- 读取 P99 **903ms** → **超标约 1.8 倍**

**归因（已核实，非猜测）**：

1. **SQLite 单写入者串行化是主因**。每个写请求都在 `session_scope()` 里**同步**落库
   （`save_telemetry` + `save_candidates`）。SQLite 同一时刻只允许一个写事务，60 个
   并发 worker 抢一把写锁 → 排队延迟主导 P99。实测吞吐 35 req/s ≈ SQLite 在本机
   单文件上的串行写能力上界。
2. 读取同样受 SQLite 限制（`COUNT(*)` 聚合 + 列表查询与写入争锁）。
3. 2 个 `207`（`persist_failed`）出现在写入路径 → 并发写锁竞争已开始产生落库失败，
   印证瓶颈在存储层而非计算层。

**结论**：当前数字反映的是**本机 SQLite 形态**，不是代码的算法瓶颈。热路径计算
（网关→解析→检测）本身是毫秒级（见 20 worker 小规模试跑 P50 22ms）。要验证
SLO ≤500ms，**必须在 PostgreSQL + 容器环境重测**——这是下一步，不是本机可得的结论。

## 下一步

- [ ] 在 `make up`（PostgreSQL 容器）下重跑 `bench.sh`，用 `DD_DATABASE_URL` 指向 PG，填第二张表
- [ ] PostgreSQL 形态若仍超 500ms，再排查索引/连接池/落库批量化
- [ ] 考虑写入路径**批量化落库**（把单请求多次 INSERT 合并），单列一项优化任务

## 诚实边界（引用 `docs/p4/08_soft_realtime.md`）

- **硬实时 <1ms 非目标**：Python + FastAPI + 关系库往返物理上做不到，改述为软实时。
- **本机数字 ≠ 生产数字**：环境差异大，生产须在目标环境重跑。
- **单机 loadgen 是近似**：loadgen 自身可能成为瓶颈，数字是下界近似。
- 本文档**不填**未跑过的数字。
