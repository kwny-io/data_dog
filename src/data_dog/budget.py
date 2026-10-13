"""租户级 LLM **用量/花费硬约束**——不是"能看见"，是"超了就停"。

为什么需要它
-----------
observability.py 能算出每次调查花了多少 token / 多少钱（价格表 + UsageTracker），
但**没有任何地方拦得住它**：一个租户刷候选就能把账单刷穿，而且是事后才发现。
治理和观测是两件事——观测回答"花了多少"，治理回答"还能不能花"。

设计
----
- 计数器放在 StateStore（memory / postgres / redis 共用同一个 mutate 原子原语，
  天然跨副本一致，不引入新后端）。
- **两个周期各一个 key**：budget:{tenant}:day / budget:{tenant}:month。值形如
  {"period": "20261013", "tokens": n, "usd": x, "calls": n}；**跨周期首次访问自动归零**
  ——不需要定时任务，key 数量也是 O(租户数) 而不是 O(天数)。
- 两个闸门：**接活前**（端点 / worker：省得白花钱）与**每次模型调用前**（provider 包装器：
  真正花钱的那一处）。后者保证"一次很长的调查中途也会被叫停"。
- 扣费在**调用之后**按 provider 的真实用量记（UsageTracker 的 CallStat），不做估算。

诚实边界
--------
- 扣费发生在**调用之后**，所以单次调用**必然**可能超出上限（最多超一次调用）。
  要做到"零超出"得在请求前按 max_tokens 预授权，还要处理上游不回 usage 的情况——
  复杂度不抵收益。这里给的是"最多超一次调用"的硬约束，且在**下一次调用前**一定拦住。
- 金额是按 observability 的价格表**估算**，**不是账单**；未定价模型只计 token 不计钱。
  要真账单请对供应商账单做对账。
- 阈值是**全局配置**，不是每租户一份合同（与保留策略同一取舍）。
- 计数器不随租户删除而消失（每租户固定 2 个 key），随 state_kv 一起被检查点管理。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from . import settings
from .core import metrics
from .state import get_state_store


class BudgetExceeded(RuntimeError):
    """额度耗尽。带上人话原因（进 HTTP detail / 运行日志的 error_detail）。"""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Limits:
    """各周期上限；**0 = 不限**（默认全 0 → 行为与加这个模块之前完全一致）。"""

    tokens_per_day: int = 0
    usd_per_day: float = 0.0
    tokens_per_month: int = 0
    usd_per_month: float = 0.0

    def any(self) -> bool:
        return bool(self.tokens_per_day or self.usd_per_day
                    or self.tokens_per_month or self.usd_per_month)


def limits() -> Limits:
    return Limits(
        tokens_per_day=settings.llm_budget_tokens_per_day(),
        usd_per_day=settings.llm_budget_usd_per_day(),
        tokens_per_month=settings.llm_budget_tokens_per_month(),
        usd_per_month=settings.llm_budget_usd_per_month(),
    )


def _period(now: float | None = None) -> tuple[str, str]:
    t = time.gmtime(time.time() if now is None else now)
    return time.strftime("%Y%m%d", t), time.strftime("%Y%m", t)


def _keys(tenant: str, now: float | None = None) -> tuple[str, str, str, str]:
    day, month = _period(now)
    return (f"budget:{tenant}:day", day, f"budget:{tenant}:month", month)


def _zero(period: str) -> dict[str, Any]:
    return {"period": period, "tokens": 0, "usd": 0.0, "calls": 0}


def _roll(cur: Any, period: str) -> dict[str, Any]:
    """跨周期归零：周期不同就当作空（**不需要定时任务**）。"""
    if not isinstance(cur, dict) or cur.get("period") != period:
        return _zero(period)
    return {"period": period,
            "tokens": int(cur.get("tokens", 0)),
            "usd": float(cur.get("usd", 0.0)),
            "calls": int(cur.get("calls", 0))}


def snapshot(tenant: str, now: float | None = None) -> dict[str, Any]:
    """只读快照：当前用量 + 上限 + 是否已超。前端/运维"还剩多少额度"就看它。"""
    st = get_state_store()
    day_key, day, month_key, month = _keys(tenant, now)
    d = _roll(st.get(day_key), day)
    m = _roll(st.get(month_key), month)
    lim = limits()
    return {"tenant": tenant,
            "limits": {"tokens_per_day": lim.tokens_per_day,
                       "usd_per_day": lim.usd_per_day,
                       "tokens_per_month": lim.tokens_per_month,
                       "usd_per_month": lim.usd_per_month},
            "day": d, "month": m,
            "enforced": settings.llm_budget_enforce(),
            "exceeded": _reason(d, m, lim) is not None,
            "reason": _reason(d, m, lim) or ""}


def _reason(d: dict[str, Any], m: dict[str, Any], lim: Limits) -> str | None:
    """返回"为什么超了"（人话）；没超返回 None。日/月各自看 token 与金额。"""
    if lim.tokens_per_day and d["tokens"] >= lim.tokens_per_day:
        return f"当日 token 用量 {d['tokens']} 已达上限 {lim.tokens_per_day}"
    if lim.usd_per_day and d["usd"] >= lim.usd_per_day:
        return f"当日花费 ${d['usd']:.4f} 已达上限 ${lim.usd_per_day:.4f}"
    if lim.tokens_per_month and m["tokens"] >= lim.tokens_per_month:
        return f"当月 token 用量 {m['tokens']} 已达上限 {lim.tokens_per_month}"
    if lim.usd_per_month and m["usd"] >= lim.usd_per_month:
        return f"当月花费 ${m['usd']:.4f} 已达上限 ${lim.usd_per_month:.4f}"
    return None


def ensure(tenant: str, *, stage: str = "precheck", now: float | None = None) -> None:
    """闸门：额度耗尽 → 抛 BudgetExceeded（调用方翻成 429 / 失败码）。

    DD_LLM_BUDGET_ENFORCE=0 时**只记不拦**（灰度：先看用量，再开闸）。
    """
    lim = limits()
    if not lim.any() or not settings.llm_budget_enforce():
        return
    st = get_state_store()
    day_key, day, month_key, month = _keys(tenant, now)
    reason = _reason(_roll(st.get(day_key), day), _roll(st.get(month_key), month), lim)
    if reason:
        metrics.llm_budget_exceeded_total.labels(stage=stage).inc()
        raise BudgetExceeded(reason)


def charge(tenant: str, *, tokens: int = 0, usd: float = 0.0, calls: int = 1,
           now: float | None = None) -> None:
    """记一次用量（调用后）。两个周期 key **一次提交**写完（mutate_many）。"""
    if not tokens and not usd and not calls:
        return
    st = get_state_store()
    day_key, day, month_key, month = _keys(tenant, now)

    def mk(period: str):
        def fn(cur: Any) -> tuple[Any, Any]:
            v = _roll(cur, period)
            v["tokens"] += int(tokens)
            v["usd"] = round(v["usd"] + float(usd), 8)
            v["calls"] += int(calls)
            return v, None
        return fn

    st.mutate_many([(day_key, mk(day)), (month_key, mk(month))])
    if tokens:
        metrics.llm_budget_tokens_total.inc(int(tokens))
    if usd:
        metrics.llm_budget_usd_total.inc(float(usd))
