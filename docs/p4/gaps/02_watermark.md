# G2 事件时间 / 水位线 / 迟到事件

## 1. 目标

让窗口计算基于**事件时间**（`occurred_at`）而非**到达顺序**，并对**迟到/乱序**数据有
确定、可解释的处理——否则一次网络重传导致的乱序会**算错窗口**（均值/斜率失真、
候选漏合并或错合并）。

## 2. 现状（已核实 —— 比预想好）

| 已有 | 位置 | 说明 |
|---|---|---|
| 双时间戳 | `gateway.py:24,164` | 事件同时带 `occurred_at`（事件时间）+ `received_at`（到达时间），均必填 |
| **合并窗口已用事件时间** | `candidate.py:23-45,89-98,123` | `_find_group` 用 `|now_dt - g.last_dt| <= merge_window_s` 判同窗；`_parse_time(occurred_at)` |
| **保守合并（abs）** | `candidate.py:97` | 用 `abs(delta) <= merge_window_s` → **早到的迟到事件也会被并入当前组**（已有"迟到容忍"语义） |
| 共享版同参 | `candidate_shared.py:127` | 同样 `abs(...) <= merge_window_s` |

**缺**（真实缺口）：
- **`WindowBuffer` 按到达顺序**（`chain.py:120-147`，定长 `deque`，`append`+`pop(0)`），
  **不按事件时间排序** → 乱序到达会污染 `mean/slope/max/last`。
- **无 watermark / allowed_lateness / 迟到侧输出**（全仓库 grep 零命中）。
- `abs()` 合并是**无界容忍**：一个非常晚的事件（如 1 小时前的）也可能被并入，因为 abs
  只看距离不看方向与水位。

## 3. 设计

### 3.1 水位线（watermark）

定义：`watermark = max_seen_occurred_at - allowed_lateness`。含义——**不再期待**早于
watermark 的事件。

```python
# data_plane/watermark.py（新建）
@dataclass
class Watermark:
    allowed_lateness_s: float = 30.0
    _max_seen: float = 0.0
    def observe(self, occurred_at_s: float) -> None:  # 单调推进
        self._max_seen = max(self._max_seen, occurred_at_s)
    @property
    def value(self) -> float:
        return self._max_seen - self.allowed_lateness_s
    def is_late(self, occurred_at_s: float) -> bool:
        return occurred_at_s < self.value
```

- **单调**：水位线只前进不后退（`max`）。
- 每源（每 StreamRunner / 每 tenant 分片）**各一个**水位线。

### 3.2 窗口按事件时间排序（改 `WindowBuffer`）

- `WindowBuffer` 改为**按 `occurred_at` 有序插入**（而非 append 顺序）；
  迟到但在容忍窗口内的事件**插入到正确位置**；
- 超出容忍（`is_late`）→ **不插入**，走**迟到侧输出**（见 §3.3）；
- `size` 仍定长（保留最近 N 个**按事件时间**的样本）。
- **保持确定性**：排序键 `(occurred_at, sequence)`，`sequence` 破同秒平局。

### 3.3 迟到侧输出（side output）

迟到事件**不静默丢**（那是最难排查的静默错误），而是：

- 计 `data_dog_events_late_total{source}`（指标）；
- 可选：投到**迟到通道**（如 `late/` 前缀 topic 或单独表），供对账；
- **候选合并**：迟到的单条事件若落在某已关闭组的窗口内 → 记 `late_merged` 指标
  （可选并入，不改已产出候选的确定性 ID）。

### 3.4 合并窗口：`abs` → 有界容忍

把 `candidate.py:97` 的 `abs(delta) <= merge_window_s` 改为：
- **正常**：`0 <= (now - last) <= merge_window_s`（顺序合并）；
- **迟到**：`-allowed_lateness_s <= delta < 0` 且 `now >= watermark` → 并入并计 `late_merged`；
- **太晚**（`now < watermark`）→ 开新组（不与旧组混）。

> 注意：这会**改变**"无界 abs"的既有行为。**须逐条核对现有测试**（`test_data_plane*`、
> `test_stream*` 里合并相关的断言），确保是"收紧"而非"破坏"。

### 3.5 触发：水位线驱动 flush

流式消费的 flush 不再只靠"源安静"，也靠**水位线推进**：
`watermark >= 组窗口末端 + merge_window_s` → 该组"可关"，可安全产出候选并落库。

## 4. 要建/要改的文件

| 文件 | 动作 |
|---|---|
| `src/data_dog/data_plane/watermark.py` | **新建**：Watermark |
| `src/data_dog/data_plane/chain.py` | 改：`WindowBuffer` 按事件时间有序 |
| `src/data_dog/data_plane/candidate.py` | 改：`_find_group` 引入水位线 + 有界迟到容忍 |
| `src/data_dog/data_plane/candidate_shared.py` | 改：同步语义 |
| `src/data_dog/stream/runner.py` | 改：喂水位线 + 水位线驱动 flush |
| `src/data_dog/settings.py` | 加 `DD_ALLOWED_LATENESS_S`（默认 30） |
| `src/data_dog/core/metrics.py` | 加 `events_late_total`、`late_merged_total` |
| `tests/test_watermark.py` | **新建** |

## 5. 验收

1. **乱序重排**：事件按 `occurred_at` 乱序到达 → `WindowBuffer` 内容按事件时间有序 →
   `slope/mean` 与顺序到达时**一致**。
2. **水位线单调**：喂乱序事件，`watermark.value` 只增不减。
3. **迟到容忍**：在容忍窗内的迟到事件被并入（`late_merged` 计数）；超容忍的被拒（`events_late` 计数）。
4. **合并不再无界**：非常晚（< watermark）的事件**不与旧组混**（新开组）。
5. **确定性**：同输入序列（任意到达顺序）→ 同候选集、同 `candidate_id`。
6. **水位线驱动 flush**：组可关时立即落库（低流量可见性不退化）。
7. **零回归**：现有合并/窗口断言全绿（若收紧语义，逐条核对并更新**理由**写在测试里）。

## 6. 诚实边界

- **容忍窗口是权衡**：太小 → 迟到数据被拒（漏合并）；太大 → 内存与延迟增加。默认 30s，
  按真实数据调。
- **不保证 exactly-once**：水位线 + 侧输出是**工程近似**，严格语义需事务性流处理。
- **候选 ID 稳定性的约束**：并入迟到事件会改变组的 `sample_count` 等，但 `candidate_id`
  由组 key（不含时间）派生，**ID 稳定**——这是有意的（幂等 upsert 不产生重复候选）。
- **单机/多副本一致**：水位线目前**每副本各一份**；多副本下需共享或按分片（对齐 P4-5）。

## 7. 依赖

- 可独立做（G2 是纯 `data_plane` 内改动）。
- 与 **P4-5 分片** 衔接：水位线应按分片维护。
