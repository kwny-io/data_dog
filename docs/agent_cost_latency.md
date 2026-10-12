# Agent 调查的成本与延迟（实测分布）

> 目的：把 `docs/slo.md` §5 的"单次调查成本 ≤ ¥0.05 / 时延 P95 ≤ 10s"从**设计目标**变成
> **实测分布**。工具：`scripts/investigation_cost.py`（真 LLM，n=18，合成样本）。

## 0. 先修一个必须先修的坑：证据里泄露了 ground truth

做这件事时发现 `SynthSample.telemetry()` 的返回里带着 `fault_ground_truth`（含故障类型 /
描述）与 `valid_mask`（12000 点逐点掩码），而 `SyntheticEvidenceSource` **原样喂给 Agent**：

- `fault_ground_truth` = **把答案递给模型**（评测污染）。此前没暴露，是因为**确定性 provider
  不读它**；但真 LLM 评测会把整包 JSON 喂进去——**`dataset_eval` 的历史 LLM 数字因此不可信，
  须重跑**。
- `valid_mask` = ~2 万 tokens / 次（60KB payload），纯浪费。

已修（在**证据源边界**剔除这两个字段）+ 回归测试 `tests/test_evidence_no_leak.py`。

## 1. 成本（n=18，model=deepseek-v4.1）

| | P50 | P95 | P99 | 均值 |
|---|---|---|---|---|
| 调用次数 / 调查 | 3 | 3 | — | — |
| 总 tokens | 8,810 | 9,563 | 11,016 | — |
| 成本（¥，参考价 0.27/1.10 USD/1M） | 0.0290 | 0.0349 | **0.0436** | 0.0307 |

- **成本 SLO 达标**：P99 **¥0.044 < 目标 ¥0.05**；**超上限（>¥0.20）率 0**。
- 修复前同口径 P95 ≈ **13 万 tokens**（≈14×）——去掉逐点掩码的直接效果。
- ⚠️ 价格是**参考价**（DeepSeek 公开价）。自带端点用 `DD_LLM_PRICE_IN/OUT` 声明真实价，
  否则成本显示"未定价"（`cost_estimated=True`）——**绝不假装知道价钱**。

## 2. 延迟（同一批）

| | P50 | P95 | P99 |
|---|---|---|---|
| LLM 网络耗时 | 36.5 s | 59.5 s | 75.7 s |
| 调查墙钟 | 36.5 s | 59.5 s | 75.7 s |

- **时延 SLO 严重未达标**：P95 **59.5s** vs 目标 **10s**（≈6×）。墙钟几乎全是 LLM 网络
  （36.5/36.5、59.5/59.5）——即 **Agent 时延 ≈ LLM 时延**，与数据面 / 落库无关
  （数据面 ~0.04ms、HTTP+落库 ~5.4ms，见 `docs/write_path_batching.md` §9）。
- 成因：本轮是**推理模型**（`deepseek-v4.1`，每次调用先产 `reasoning_content`），单次 12~20s；
  一条调查 3 次调用 → 36s 起步。**换非推理模型 / 流式 / 并行可显著降。**
- 这也是"**模型选型 = 时延选型**"的直接证据：同一个 Agent，换个模型，P95 从 10s 量级变成
  60s 量级。

## 3. 边界

- n=18（每类 3），CI 很宽；数字是**同一窗口的相对对照**，不是常量。
- 合成样本、单机、公开网关；真实部署需在目标环境复测。
- 复现：`python scripts/investigation_cost.py --n-per-class 3 --workers 4 [--price-in … --price-out …]`。
