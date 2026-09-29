"""跨副本共享状态（限流桶、候选分组）。

为什么需要这一层
----------------
水平扩展的**唯一硬约束**是"有状态算子不能只活在进程内"：多副本时，A 实例
限流只挡得住 A 看到的流量，B 实例的窗口/候选分组与 A 各算各的 → 同一设备
在两个副本上被当成两段独立事件，候选重复、限流失效。要水平扩展，状态必须
外置到**所有副本都能读到同一份**的地方。

设计
----
- **接口不变，后端可插拔**：调用点只认 `StateStore`，后端由 `DD_STATE_BACKEND`
  选（`memory` / `postgres` / `redis`）。切后端是配置动作，调用点代码零改动。
- **默认 memory**：单副本、测试、离线演示零依赖跑通（与现状行为一致）。
- **postgres**：已有 PG 支持与迁移链，零新依赖，小/中流量首选。
- **redis**：显式开启，适合高频限流检查成为热路径瓶颈时。

诚实边界
--------
- SQLite 后端不支持（无跨进程共享语义），memory 后端在多副本下**不保证一致**
  ——单副本用 memory，多副本必须选 postgres/redis，否则限流与候选合并会各算各的。
"""
from __future__ import annotations

from .base import MutateFn, StateStore
from .factory import get_rate_store, get_state_store, reset_state_store

__all__ = [
    "MutateFn",
    "StateStore",
    "get_rate_store",
    "get_state_store",
    "reset_state_store",
]
