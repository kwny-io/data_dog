"""P4-6 跨地域多活：确定性收敛与冲突消解。

异步双向复制下，同一逻辑实体可能在两个区域被分别写入，复制到对端后需
**确定性地**合并成同一结果——无论复制方向与到达顺序如何，最终值必须一致。
本包只做**应用层可解**的部分：确定性 ID 收敛判定 + 可变状态（告警）冲突消解。
区域编排、PG 逻辑复制、DNS 切换等基础设施部分**不在代码内**（见
`docs/p4/06_multi_region.md` §诚实边界）。

诚实边界：本包**不**提供 RPO=0（异步复制本质），**不**做脑裂自动恢复
（主选举依赖外部共识）。
"""
from __future__ import annotations

from .conflict import (
    SEVERITY_RANK,
    Conflict,
    merge_alert_state,
    resolve_severity,
    resolve_two,
)

__all__ = [
    "SEVERITY_RANK",
    "Conflict",
    "merge_alert_state",
    "resolve_severity",
    "resolve_two",
]
