"""提交幂等（Idempotency-Key）：把"重复提交 = 重复花钱"在入口掐掉。

为什么需要它
-----------
POST /v1/investigations 一次就是一次真实 LLM 调用（10–60s、真金白银）。客户端超时重试、
用户连点两下、上游 at-least-once 投递——都会变成**同一件事做两遍**：两份 LLM 花费。
报告最终会 upsert 到同一个 investigation_id（所以"结果"看不出重复），**钱已经花了**。

语义（对齐业界 Idempotency-Key 的通行口径，只取需要的部分）
--------------------------------------------------------
- 调用方给 `Idempotency-Key` 头 → 在 `TTL` 窗口内，(租户, key) 唯一。
- 同一 key 的第二次请求：
  · 首次已完成 → 返回**同一份结果**（带 `idempotent_replay: true`），不再调模型；
  · 首次仍在跑 → `409` + `Retry-After`（**不重跑**，也不假装成功）。
- **原子占位**：占位走 `StateStore.mutate`，两个并发重复请求只有一个能拿到 `new`，
  另一个必然看到 `inflight` —— 靠数据库/Redis 的原子性，不靠进程内锁（多副本也成立）。
- 失败（队列满 / 额度不足 / 执行前就报错）→ **释放占位**，否则调用方连正确重试的机会都没有。

诚实边界
--------
- 过期靠**读时判断**（记 `at`），不依赖后端的 TTL 原语——`StateStore` 契约里没有 TTL，
  而为一个功能去扩后端契约不划算。代价：过期 key 会留到下次同 key 访问才被覆盖。
- 幂等键**不绑定请求体**：同一个 key 配不同的 candidate_id 会拿到首次的结果。
  这是刻意的（与主流实现一致），但要求调用方"一个 key 只用于一次提交"。
- 未提供该头 → 完全维持旧行为（零回归）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..state import get_state_store


@dataclass(frozen=True)
class Decision:
    """占位结果：new（首次）/ done（已完成，带结果引用）/ inflight（首次仍在跑）。"""

    state: str
    ref: dict[str, Any] = field(default_factory=dict)


def _k(tenant: str, key: str) -> str:
    return f"idem:{tenant}:{key}"


def reserve(tenant: str, key: str | None, *, ttl_s: float, kind: str = "unknown",
            now: float | None = None) -> Decision:
    """原子占位。`key` 为空或 `ttl_s<=0` → 直接返回 new（幂等关闭，旧行为）。"""
    if not key or ttl_s <= 0:
        return Decision("new")
    ts = time.time() if now is None else now
    st = get_state_store()

    def fn(cur: Any) -> tuple[Any, Any]:
        if isinstance(cur, dict) and ts - float(cur.get("at", 0.0)) <= ttl_s:
            if cur.get("state") == "done":
                return cur, Decision("done", dict(cur.get("ref") or {}))
            return cur, Decision("inflight", dict(cur.get("ref") or {}))
        fresh = {"at": ts, "kind": kind, "state": "running", "ref": {}}
        return fresh, Decision("new")

    return st.mutate(_k(tenant, key), fn)


def complete(tenant: str, key: str | None, ref: dict[str, Any], *,
             ttl_s: float, now: float | None = None) -> None:
    """标记完成并记住结果引用（job_id / investigation_id），供重放直接返回。"""
    if not key or ttl_s <= 0:
        return
    ts = time.time() if now is None else now
    st = get_state_store()

    def fn(cur: Any) -> tuple[Any, Any]:
        base = cur if isinstance(cur, dict) else {"kind": ""}
        return {**base, "at": ts, "state": "done", "ref": dict(ref)}, None

    st.mutate(_k(tenant, key), fn)


def release(tenant: str, key: str | None) -> None:
    """释放占位（首次提交失败时调用）。只有在 running 时才删——别把已完成的记录删掉。"""
    if not key:
        return
    st = get_state_store()

    def fn(cur: Any) -> tuple[Any, Any]:
        if isinstance(cur, dict) and cur.get("state") == "done":
            return cur, False
        return None, True

    removed = st.mutate(_k(tenant, key), fn)
    if removed:
        st.delete(_k(tenant, key))
