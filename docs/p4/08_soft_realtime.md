# P4-8 软实时延迟目标（P99 实测）

> **建议最先做**：最便宜，且**给所有性能声明兜底**——没有实测就不能说"快"。

## 1. 目标

把性能主张从"感觉很快"变成**实测 P99 数字**。同时**如实改述**：硬实时 <1ms 不可达
（Python + FastAPI + 关系库的物理约束），项目目标定为**软实时**（热路径 P99 有界）。

## 2. 现状（已核实）

| 已有 | 位置 | 说明 |
|---|---|---|
| 请求直方图 | `core/metrics.py` | `http_request_duration_seconds = Histogram(buckets=0.005..2.5, label=method,path)` |
| SLO 文档 | `docs/slo.md:60` | 目标 **P99 ≤ 500ms**（已声明，但**缺实测基线**） |
| 探针 | `/healthz` `/readyz` `/metrics` | 就绪 |

**缺**：loadgen / benchmark 工具；P99 实测基线脚本；无压测 CI。

## 3. 设计

### 3.1 loadgen（轻量，stdlib 优先）

```python
# tools/loadgen.py（新建）
# 不进 src/（非生产依赖）；纯标准库 + 可选 requests
# 用法：python tools/loadgen.py --url http://localhost:8000 --devices 120 \
#         --rps 500 --duration 60 --tenant t1
```

- 模拟 **N 台设备**并发打 `POST /v1/telemetry`（可复用 P4-5 的分片键）；
- 记录**客户端侧**延迟（端到端，含网络）→ 输出 **P50/P95/P99/max** + 吞吐；
- 同时读服务端 `/metrics` 的 `http_request_duration_seconds` **对照**（两侧一致才可信）；
- 支持**只读**模式（打 `/v1/candidates`）与**写**模式。

### 3.2 基准脚本 + 记录

```
tools/bench.sh               # 起服务 → loadgen → 抓指标 → 输出报告
benchmarks/baseline.md       # 实测结果表（环境 + 数字 + 日期），随代码更新
```

基线表（示例格式，数字**实测后填**）：

| 场景 | 设备数 | RPS | P50 | P95 | P99 | 吞吐 | 环境 |
|---|---|---|---|---|---|---|---|
| 遥测写入 | 120 | 500 | ? | ? | ? | ? | 本机/容器 |
| 候选读取 | — | 200 | ? | ? | ? | ? | 本机/容器 |
| 调查（离线 provider） | — | 5 | ? | ? | ? | ? | 本机 |

### 3.3 指标补全

- 现有直方图 buckets（0.005–2.5s）覆盖 P99≤500ms 目标，**够用**；
- 补 **分路径** 的 P99 计算（`histogram_quantile(0.99, ...)` PromQL 示例写入文档）；
- 补**流式消费**指标已就绪（`stream_*`）——加消费延迟直方图（消息产生→落库）。

### 3.4 CI（可选）

- **不在每次 CI 跑压测**（噪声大、耗资源）；改为**手动触发**的 workflow（`workflow_dispatch`）
  或 nightly，跑 `bench.sh` 并存档结果。

## 4. 要建/要改的文件

| 文件 | 动作 |
|---|---|
| `tools/loadgen.py` | **新建**：并发压测 |
| `tools/bench.sh` | **新建**：一键基准 |
| `benchmarks/baseline.md` | **新建**：实测结果表 |
| `src/data_dog/core/metrics.py` | 加消费延迟直方图（可选） |
| `docs/slo.md` | 更新：硬实时→软实时改述 + 指向 baseline.md |
| `Makefile` | 加 `bench` 目标 |
| `.github/workflows/bench.yml` | **新建**（手动/nightly） |

## 5. 验收

1. **能跑**：`make bench` 一键出报告（P50/P95/P99 + 吞吐）。
2. **两侧一致**：客户端 P99 与服务端直方图 P99 量级一致。
3. **基线入库**：`benchmarks/baseline.md` 有**真实数字**（非占位）。
4. **SLO 对齐**：实测 P99 与 `slo.md` 目标对比，**达标或如实记录差距**。
5. **热路径零 LLM 可验证**：压测期间 `/v1/telemetry` 不触发任何 LLM 调用（断言 provider 未调）。

## 6. 诚实边界

- **硬实时 <1ms 非目标**：Python 解释器 + HTTP + DB 往返不可能 <1ms。**改述为软实时**
  （热路径 P99 有界，如 ≤500ms）。**这是语言/架构的真实约束，不是不努力**。
- **本机数字 ≠ 生产数字**：环境（CPU/网络/托管）差异大；生产需在**目标环境**重跑。
- **压测工具本身是近似**：单机 loadgen 会成为瓶颈时需多机打流。
- **数字须实测**：本文档与 README **不填**未跑过的吞吐数。

## 7. 依赖

无。**建议本批最先做**——为 P4-5（吞吐实测）、P4-4（HPA 阈值）提供数字依据。
