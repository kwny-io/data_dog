# P4-7 退化趋势预测（ETA + 区间，非精确 RUL）

## 1. 目标

预测性维护：在电机**真的过热前**给出「预计 N 小时后到达阈值 ± 区间」的预警，
让运维从"救火"转"预防"。**诚实定位**：这是**趋势外推 + 到达时间估计**，
**不是**研究级精确剩余寿命（RUL）——后者需要退化物模型 + 全生命周期数据。

## 2. 现状（已核实）

| 已有 | 位置 | 说明 |
|---|---|---|
| 即时降级决策 | `agent/degradation.py` `DegradationPolicy.decide` | 基于**硬规则/严重度**的即时判定，**非趋势预测** |
| 窗口特征 | `data_plane/chain.py` | 窗口已有 `slope`/`delta`/均值等 |
| 基线 | Enricher | 同工况基线（可作对照） |

**缺**：predict/rul/forecast/eta 全部没有；`slo.md:115` 明确列为非目标（不含**精确** RUL）。

## 3. 设计

### 3.1 方法：线性/单调趋势外推 + ETA

核心是**便宜、确定、可解释**——不引入 ML 黑盒（与项目"热路径零模型"精神一致）：

1. **取退化信号**（如电机温度）的**近窗口序列**（如最近 10min 采样）。
2. **稳健拟合斜率**：用 **Theil–Sen**（中位斜率）而非最小二乘——抗离群点（单次尖峰
   不该毁掉预测）。
3. **外推到达阈值的时间**：`ETA = (threshold - current) / slope`（slope>0 时）。
4. **区间**：由拟合残差 + 斜率不确定度给出 `[ETA_lo, ETA_hi]`（如 ±1σ 或 bootstrap）。
5. **置信门槛**：斜率不显著 / 样本不足 / 非单调 → **不预测**（失败关闭，宁缺勿猜）。

```python
# data_plane/forecast.py（新建）
@dataclass(frozen=True)
class DegradationForecast:
    signal: str
    current: float
    threshold: float
    slope_per_hour: float          # 中位斜率（带符号）
    eta_hours: float | None        # None = 无法预测
    eta_lo_hours: float | None
    eta_hi_hours: float | None
    confidence: float              # 0..1
    method: str                    # "theil-sen+residual"

def forecast_to_threshold(
    series: Sequence[tuple[float, float]],  # (t_seconds, value)
    threshold: float, *, min_samples: int = 6, min_confidence: float = 0.5,
) -> DegradationForecast: ...
```

### 3.2 何时触发

- **不阻塞热路径**：预测是**周期任务**（对活跃设备的近窗口跑一次，如每分钟），
  或由候选触发（某候选已命中 → 对同信号补一条预测）。
- **并入候选人/证据**：预测作为候选的 **附加证据**（`evidence` 里一条 forecast 记录），
  不改变候选本身的确定性判定（**上报仍由策略门决定**）。
- **预警等级**：`ETA < DD_FORECAST_HORIZON_H`（如 24h）且在区间内 → 标为"前瞻预警"。

### 3.3 端点 / 展示

```
GET /v1/devices/{device_sn}/forecast        该设备所有信号的预测
GET /v1/candidates/{id}/forecast            候选相关信号的预测（若已算）
```

（预测结果可**不入库**、按需现算，也可缓存；按需现算更简单、无一致性问题。）

## 4. 要建/要改的文件

| 文件 | 动作 |
|---|---|
| `src/data_dog/data_plane/forecast.py` | **新建**：Theil–Sen + ETA + 区间 |
| `src/data_dog/data_plane/chain.py` 或新模块 | 接：从窗口取序列喂 forecast |
| `src/data_dog/service/app.py` | 加 2 个读取端点（末尾新分节） |
| `src/data_dog/settings.py` | 加 `DD_FORECAST_HORIZON_H`、`DD_FORECAST_MIN_CONF` |
| `src/data_dog/core/metrics.py` | 加预测产出/置信度分布指标 |
| `tests/test_forecast.py` | **新建** |

## 5. 验收

1. **增长序列**：构造线性升温序列 → ETA 接近解析值（误差 < 10%）。
2. **抗离群**：插入单个尖峰 → ETA 基本不变（Theil–Sen 的抗性）。
3. **无法预测**：样本不足 / 斜率≈0 / 非单调 → `eta_hours is None`（**不猜**）。
4. **区间合理**：残差大 → 区间宽；残差小 → 区间窄。
5. **热路径无影响**：预测不改变 `/v1/telemetry` 的延迟与候选判定。
6. **确定性**：同输入序列 → 同预测（可复现）。

## 6. 诚实边界

- **不是精确 RUL**：输出「到阈值的 ETA ± 区间」，**不预测确切故障日期**。
- **线性假设**：对**渐变型**退化（热退化、磨损）有效；对**突发型**故障（堵转、短路）
  无效——那类靠即时检测（已有），不靠外推。
- **需足够采样密度**：稀疏采样下不预测。
- **不外推太远**：超出 `DD_FORECAST_HORIZON_H` 不给数（外推越远越不可信）。
- **不给控制回路用**：本体安全决策**不**依赖本预测（与硬实时非目标一致）。

## 7. 依赖

无。可独立并行。与 `agent/degradation.py` **互补**（一个是即时规则，一个是趋势外推），
不替换。
