"""冲突消解：跨区域复制后把两份状态**确定性**合并成一份。

为什么需要
----------
P4-6 的复制是**异步双向**的：区域 A 改了某告警、区域 B 也改了它。两份更新
先后到达对端，若合并规则不确定（依赖到达顺序、依赖本机时钟），两个区域会
收敛到**不同**的最终状态 → 永久分歧。本模块把合并规则定死为**纯函数**：
同样的两份输入，任何区域、任何顺序，算出**同一个**结果。

两类数据，两种处理
------------------
- **确定性 ID 的实体**（候选/事件/幂等键）：`event_id` 由输入三要素 `sha256`
  派生（`alerting/outbox.py`）——两区域对同一 Episode 各自算出**同一个**
  `event_id`。故复制后是**同一条记录**，天然收敛，无需消解。本模块不再处理。
- **可变状态**（告警状态机、severity）：同一 `event_id` 在两个区域被推进到
  不同状态 → 需要消解。

消解规则（确定性，与顺序无关）
------------------------------
1. **有因果序**：用 `event_revision`（`alerting/lifecycle.py` 每次转移递增）判定。
   revision 大者**见证**了对方（或更多后续转移）→ 直接取胜。这是因果序：
   高 revision 是低 revision 的后继，取高即"取最新已知状态"。
2. **真并发**（`revision` 相等）：无因果关系的并列更新 → 按固定策略：
   - **告警状态取更严重**：`severity` 高者胜（乐观偏向安全——宁可多报重报，
     不可漏报重症）。severity 词表 `critical > warning > info`
     （`alerting/policy.py`）。
   - **`ack`（确认）取先到**：ack 是"已有真人接手"的不可逆事实，先到即有效
     ——后到的重复 ack 不改变已确认状态。

幂等与顺序无关
--------------
`resolve_two(a, b) == resolve_two(b, a)`：交换输入不改变输出。因此无论复制
方向、无论谁先到达，两区域都收敛到同一结果（测试 `test_conflict_resolution.py`
的可重放断言钉住）。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# severity 从轻到重（与 alerting/policy.py 的消解词表一致）
SEVERITY_RANK: dict[str, int] = {"info": 0, "warning": 1, "critical": 2}


def resolve_severity(a: str, b: str) -> str:
    """取更高严重度（**顺序无关**）。

    未知词按 info 处理（失败关闭：不擅自抬高），且**归一**到已知词表——
    否则 `resolve_severity("bogus","info")` 在并列 rank 下会返回 `a`，使结果
    依赖参数顺序，破坏"两区域必须收敛到同一值"的根本要求。
    """
    ra, rb = SEVERITY_RANK.get(a, 0), SEVERITY_RANK.get(b, 0)
    if ra != rb:
        return a if ra > rb else b
    # rank 并列：已知词之间 rank 唯一，故必为同词；否则至少一侧是未知词 → 归一。
    if a in SEVERITY_RANK:
        return a
    if b in SEVERITY_RANK:
        return b
    return "info"          # 两侧皆未知 → 归一到最轻，不制造严重度


def _severity_holder(a: Conflict, b: Conflict) -> Conflict:
    """真并发下选 state 的持有者：severity 高者；完全并列时按 state 字典序
    （任意但**确定**——收敛只要求确定，不要求语义最优）。"""
    ka = (SEVERITY_RANK.get(a.severity, 0), a.state)
    kb = (SEVERITY_RANK.get(b.severity, 0), b.state)
    return a if ka >= kb else b


@dataclass(frozen=True)
class Conflict:
    """一份可变状态的快照（用于合并的最小字段集）。"""

    event_id: str
    state: str
    revision: int
    severity: str = "warning"
    acked: bool = False
    acked_at: float = 0.0          # ack 时间戳（真并发时取先到=更小）
    reason: str = ""               # 消解时记录为何取胜（审计）

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "state": self.state,
            "revision": self.revision,
            "severity": self.severity,
            "acked": self.acked,
            "acked_at": self.acked_at,
        }


def _merge_ack(a: Conflict, b: Conflict) -> tuple[bool, float]:
    """ack 合并：只要任一侧已 ack 即视为已 ack；时间取**先到**（更小）。

    理由：ack 是"有人接手"的不可逆事实，一旦发生不该被复制覆盖回未确认。
    """
    if not a.acked and not b.acked:
        return False, 0.0
    if a.acked and b.acked:
        return True, min(a.acked_at, b.acked_at)
    return True, a.acked_at if a.acked else b.acked_at


def resolve_two(
    a: Conflict,
    b: Conflict,
    *,
    observer: Callable[[str], None] | None = None,
) -> Conflict:
    """把同一 `event_id` 的两份状态合并为一份（**顺序无关**）。

    - 不同 `event_id`：直接报错（调用方应先按确定性 ID 分组——不同 ID 是不同实体）。
    - `revision` 不等：高者胜（因果序，见证了更多转移）。
    - `revision` 相等（真并发）：severity 取高；state 随 severity 的持有者走，
      再叠加 ack 合并（先到）。

    `observer` 可选：收到消解路径（`causal` / `concurrent`）——用于接指标。
    保持函数**纯**（默认无副作用）；副作用只在显式传入 observer 时发生。
    """
    if a.event_id != b.event_id:
        raise ValueError(
            f"只能合并同一 event_id 的两份状态，收到 {a.event_id!r} 与 {b.event_id!r}；"
            "不同 event_id 是不同实体，应按确定性 ID 分组后再合并。")

    acked, acked_at = _merge_ack(a, b)

    if a.revision != b.revision:
        winner = a if a.revision > b.revision else b
        if observer is not None:
            observer("causal")
        return Conflict(
            event_id=a.event_id,
            state=winner.state,
            revision=winner.revision,
            severity=winner.severity,
            acked=acked,
            acked_at=acked_at,
            reason=f"revision 因果序：{winner.revision} 胜（另一方 {min(a.revision, b.revision)}）",
        )

    # 真并发：severity 取高；state 随 severity 高者（乐观偏安全）
    sev = resolve_severity(a.severity, b.severity)
    holder = _severity_holder(a, b)
    if observer is not None:
        observer("concurrent")
    return Conflict(
        event_id=a.event_id,
        state=holder.state,
        revision=a.revision,
        severity=sev,
        acked=acked,
        acked_at=acked_at,
        reason=f"真并发（revision={a.revision}）：severity 取高（{sev}），state 随其持有者",
    )


def merge_alert_state(
    states: list[Conflict],
    *,
    observer: Callable[[str], None] | None = None,
) -> Conflict:
    """把同一 `event_id` 的**多份**状态折叠为一份（N 路；N=2 即 `resolve_two`）。

    折叠的顺序无关性来自 `resolve_two` 的交换律——但**结合律**需注意：
    真并发下 severity 取高、state 随持有者，再次与其他份合并时仍一致，因为
    「取最高 severity」与「取最大 revision」都是幂等可结合的运算符。

    空输入报错（无状态可合并）；单元素原样返回。
    """
    if not states:
        raise ValueError("merge_alert_state 需要至少一份状态")
    acc = states[0]
    for s in states[1:]:
        acc = resolve_two(acc, s, observer=observer)
    return acc
