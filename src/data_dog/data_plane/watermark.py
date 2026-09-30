"""事件时间水位线（G2，对齐 gap 文档 §3.1）。

为什么需要它
------------
窗口/合并此前基于**到达顺序**：一次网络重传导致的乱序会让 `mean/slope` 失真、
候选漏合并或错合并。水位线给出一个**单调前进**的时间基准，据此判断事件是否"太晚"。

定义
----
`watermark = max_seen_occurred_at - allowed_lateness`。含义——**不再期待**早于
watermark 的事件；`occurred_at < watermark` 即"迟到过多"。

设计取舍
--------
- **单调**（`max`）：水位线只前进不后退，否则"可关窗口"会回退，破坏确定性。
- **每源一份**：每 StreamRunner / 每租户分片各一个（对齐 P4-5 分片）。
- **纯内存**：本模块无 I/O、无锁；调用方（单线程消费循环）负责串行调用。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Watermark:
    """事件时间水位线。单位：epoch 秒。"""
    allowed_lateness_s: float = 30.0
    _max_seen: float = 0.0

    def observe(self, occurred_at_s: float) -> None:
        """喂入一个事件的**事件时间**（epoch 秒）；水位线单调推进。"""
        if occurred_at_s > self._max_seen:
            self._max_seen = occurred_at_s

    @property
    def value(self) -> float:
        """当前水位线：`max_seen - allowed_lateness`。"""
        return self._max_seen - self.allowed_lateness_s

    @property
    def max_seen(self) -> float:
        return self._max_seen

    def is_late(self, occurred_at_s: float) -> bool:
        """该事件是否迟到过多（早于水位线）→ 应拒（不当正常事件处理）。"""
        return occurred_at_s < self.value
