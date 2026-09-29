"""共享状态接口：一个原子原语，两个消费者。

核心原语
--------
`mutate(key, fn)`：原子地"读 key 当前值 → 应用 fn → 写回"，冲突时**重试 fn**。
把并发控制完全推给后端（内存用进程锁、PG 用行锁/乐观版本、Redis 用 WATCH），
业务逻辑（补令牌、合并分组窗口）只写一遍，不随后端重复。

为什么是这一个原语而不是给每个消费者定制方法
--------------------------------------------
限流（令牌桶）与候选分组（合并窗口归并）看着不同，本质都是"基于当前状态
原子地算出新状态 + 一个结果"。用同一个原语表达，新增消费者不必改后端契约；
后端也只需实现一次原子性。

fn 的契约（重要）
-----------------
- 必须是**纯函数**、可被多次调用（冲突重试）。不要在里面 I/O 或依赖外部可变状态。
- 入参 `current` 为 `None` 表示 key 不存在。
- 返回 `(new_value, result)`：`new_value` 是写回的 JSON 可序列化值；`result`
  是本次调用的业务结果（如"是否放行"、"归入哪个组"）。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any


class StateStore(ABC):
    """跨副本共享状态后端。所有方法须对并发调用安全。"""

    @abstractmethod
    def mutate(self, key: str, fn: Callable[[Any], tuple[Any, Any]]) -> Any:
        """原子 read-modify-write。返回 fn 算出的 result。

        实现保证：并发调用同一 key 时，每次调用看到的都是"此前所有已提交变更
        之后的**最新值**"；若在写入前检测到冲突，则重新读取并再次调用 fn。
        """

    @abstractmethod
    def get(self, key: str) -> Any:
        """读取当前值（不存在返回 None）。只读，不参与原子性保证。"""

    def delete(self, key: str) -> None:               # noqa: B027 - 有意可选，非抽象
        """删除 key（默认无操作；后端可覆写）。"""

    # ---- 建立在 mutate 之上的消费者（所有后端免费获得，逻辑只此一份）----

    def rate_limit_allow(self, key: str, *, rate_per_sec: float, burst: int,
                         now: float | None = None) -> bool:
        """令牌桶放行判定。返回 True=放行（并扣一个令牌），False=超限。

        跨副本一致：多实例共享同一 key 的桶，任一实例扣掉的令牌对其它实例可见，
        因此限流不再"各挡各的"。
        """
        ts = time.monotonic() if now is None else now

        def fn(cur: Any) -> tuple[Any, Any]:
            st = cur if isinstance(cur, dict) else {"tokens": float(burst), "last": ts}
            tokens = min(float(burst),
                         float(st["tokens"]) + (ts - float(st["last"])) * rate_per_sec)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            return {"tokens": tokens, "last": ts}, allowed

        return bool(self.mutate(f"rate:{key}", fn))

    def close(self) -> None:                          # noqa: B027 - 有意可选，非抽象
        """释放连接/资源（默认无操作）。"""


# 便捷别名：分组状态消费者复用的类型签名（文档用，非运行时约束）
MutateFn = Callable[[Any], tuple[Any, Any]]
