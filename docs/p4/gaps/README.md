# 技术缺口 —— 并行实施

> 这是**当前范围的技术缺口**（比 P4 批更近、更小），8 项。
> 文档格式与 `../p4/` 一致：现状核对 → 设计 → 文件表 → 验收 → 诚实边界。
> 每份自包含，可开并行。

## 清单

| # | 项 | 文件 | 关键位置 |
|---|---|---|---|
| G1 | 调查端点异步化 | [01_async_investigation.md](01_async_investigation.md) | `service/app.py:719-782` |
| G2 | 事件时间 / 水位线 / 迟到事件 | [02_watermark.md](02_watermark.md) | `data_plane/chain.py:120`、`candidate.py:97` |
| G3 | StateStore checkpoint / 恢复 | [03_checkpoint.md](03_checkpoint.md) | `state/base.py`、`state_kv` 表 |
| G4 | 降级队列持久化 + 接入 e2e | [04_degradation_queue.md](04_degradation_queue.md) | `agent/degradation.py:84`、`e2e/replay.py` |
| G5 | 流式源多副本协调 | [05_replica_coordination.md](05_replica_coordination.md) | `stream/bus.py`、`stream/file.py` |
| G6 | 跨部件因果时序 + 拓扑链 | [06_causal_topology.md](06_causal_topology.md) | `service/evidence.py:125`、`correlation.py` |
| G7 | 真实变更管理源适配器 | [07_change_source.md](07_change_source.md) | `service/evidence.py:161`、`evidence/store.py:138` |
| G8 | 算子验证流水线补全（编译扫描 + SBOM） | [08_verify_pipeline.md](08_verify_pipeline.md) | `operator_factory/verification.py` |

## 与 P4 批的关系

- **G5（多副本协调）是 P4-5（分片）的前置**——先有"不重复消费"，分片才有意义。
- **G8（编译扫描）是 P4-1（沙箱）的静态防线搭档**——扫描静态 + 沙箱动态。
- 其余缺口与 P4 独立。

## 统一约定（避免冲突）

- 各缺口**尽量新增独立模块**；只在 `service/app.py`、`storage/models.py` 做最小改动。
- 迁移文件名带前缀避撞号：`20260930_0900_g1_*.py`、`0910_g3_*.py` …
- 术语同 `../README.md`（热路径/调查路径/确定性 ID/失败关闭/租户作用域）。

## 本批共同诚实边界

- **不改变热路径确定性**：G1–G5 均不得让 `/v1/telemetry` 引入不确定性或 LLM。
- **不确定就失败关闭**：迟到、乱序、无协调的场景一律"保守正确"而非"猜"。
- **实测数字为准**：涉及吞吐/延迟的，由 `tools/loadgen.py`（P4-8）实测，不空口保证。
