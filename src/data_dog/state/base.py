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
from collections.abc import Callable, Sequence
from typing import Any


class StateStore(ABC):
    """跨副本共享状态后端。所有方法须对并发调用安全。"""

    @abstractmethod
    def mutate(self, key: str, fn: Callable[[Any], tuple[Any, Any]]) -> Any:
        """原子 read-modify-write。返回 fn 算出的 result。

        实现保证：并发调用同一 key 时，每次调用看到的都是"此前所有已提交变更
        之后的**最新值**"；若在写入前检测到冲突，则重新读取并再次调用 fn。
        """

    def mutate_many(self, items: Sequence[tuple[str, Callable[[Any], tuple[Any, Any]]]]
                    ) -> list[Any]:
        """**一次提交**内对多个 key 做原子 read-modify-write，返回与 items 等长的结果。

        为什么需要这个原语（提交次数就是写路径的吞吐上限）
        --------------------------------------------------
        一条遥测事件常命中多条规则 → 落在**多个**分组 key 上。逐个 `mutate` 在
        postgres/redis 后端下就是"**每个命中一次提交**"，而提交吞吐正是本机 PG 实测
        到的天花板（`docs/write_path_batching.md` §2.2：`5 次事务 × 80 req/s` 已贴住
        上限）。本原语把 N 次提交压成 1 次，**语义完全不变**——还是同样的读改写、
        同样的跨副本原子性，只是共用同一个事务。这与逐个调用的区别只在提交次数。

        语义契约
        --------
        - **整批原子**（后端须保证）：整批在一个事务 / 一个 `EXEC` 里；任一 key 失败则
          整批**不生效**（后端上抛异常）。调用方**不要**依赖"部分成功"。
          ⚠️ 基类默认实现（逐个 `mutate`）**不满足**这一条——它只保证每个 key 各自
          原子，提供的意义是"未覆写的后端仍可用"。三个内置后端（memory / postgres /
          redis）都覆写为真整批，并由 `tests/test_state_store.py` 跨后端断言守住。
        - 同一 key **可以重复出现**：按给定顺序依次应用，后一次 `fn` 看到的 `current`
          是前一次写回的值。故"同一事件多次命中同一 base"不会丢更新。
        - 跨 key **不是同一瞬间的快照**：不同 key 之间不承诺一致性（与 `snapshot`
          同一诚实口径）；要跨 key 一致性请让它们同属一个 key。
        - 默认实现逐个退化为 `mutate`（**正确，但仍是 N 次提交**）。共享内态后端
          应覆写以合并提交，这正是本原语存在的意义。
        """
        return [self.mutate(key, fn) for key, fn in items]

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

        时钟必须用**墙钟** `time.time()`，不能用 `time.monotonic()`
        ---------------------------------------------------------
        桶里存的 `last` 是**跨副本共享**的值，而 `monotonic` 是各进程私有的、
        跨机器毫无可比性（`Lease` 的模块 docstring 已经把这个道理写过一遍）。
        曾经这里用的是 `monotonic`，后果不是"限流有偏差"而是**限流变成事故**：
        主机 B（开机 58 天，monotonic≈5e6）写下空桶后，主机 A（monotonic≈6.5e5）
        算 `elapsed = 6.5e5 - 5e6 < 0`，令牌被**倒扣**到 -4.35e6 枚——按 1 req/s
        算就是这台副本**约 50 天全量 429**。而单进程测试永远发现不了：它们都显式
        传 `now=`，默认时钟一次都没被执行过。

        负的 `elapsed` 一律当 0（而不是照算）：墙钟被 NTP 回拨、或滚动升级期间
        新旧副本混跑（旧值来自 monotonic 纪元）时，`last` 可能比 `now` 还大。
        照算会把令牌扣成负数，进而把客户端锁死；当 0 只是"这一轮不补"，
        最坏退化为短暂的 fail-open，不会 fail-lockout。

        代价（诚实说明）：与租约同一条边界——依赖各副本时钟大致同步（生产须跑 NTP）。
        """
        ts = time.time() if now is None else now

        def fn(cur: Any) -> tuple[Any, Any]:
            st = cur if isinstance(cur, dict) else {"tokens": float(burst), "last": ts}
            elapsed = max(0.0, ts - float(st["last"]))
            tokens = min(float(burst), float(st["tokens"]) + elapsed * rate_per_sec)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            return {"tokens": tokens, "last": ts}, allowed

        return bool(self.mutate(f"rate:{key}", fn))

    # ---- 快照 / 恢复（G3：状态检查点）----
    # 建立在 `keys` + `get` + `mutate` 之上的消费者：后端只需实现 `keys`，
    # 快照/恢复逻辑只写一遍。

    def keys(self, prefix: str = "") -> list[str]:
        """列出所有以 `prefix` 开头的 key（无序）。后端应覆写以达到 O(prefix)。"""
        return []

    def snapshot(self, *, prefix: str = "") -> dict[str, Any]:
        """导出前缀匹配的所有 key→value（浅层）。供持久化/检查点用。

        **非原子**跨 key：导出期间若有并发写，各 key 可能取自不同瞬间。对"近似"
        状态够用；要严格一致需在导出期短暂冻结写（见 checkpoint 的诚实边界）。
        """
        return {k: self.get(k) for k in self.keys(prefix)}

    def restore(self, data: dict[str, Any], *, overwrite: bool = False) -> int:
        """从快照恢复状态。`overwrite=False` 时**只补缺失 key**（不覆盖已有）。

        返回实际写入的 key 数。逐 key 原子写（`mutate`），故与并发写不冲突。
        """
        written = 0
        for key, value in data.items():
            if not overwrite and self.get(key) is not None:
                continue
            def _apply(_cur: Any, v: Any = value) -> tuple[Any, Any]:
                return v, None
            self.mutate(key, _apply)
            written += 1
        return written

    def close(self) -> None:                          # noqa: B027 - 有意可选，非抽象
        """释放连接/资源（默认无操作）。"""


# 便捷别名：分组状态消费者复用的类型签名（文档用，非运行时约束）
MutateFn = Callable[[Any], tuple[Any, Any]]
