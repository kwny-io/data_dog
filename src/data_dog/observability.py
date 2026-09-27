"""可观测性：token 消耗 / 每步耗时 / 成本统计（后端采集 + CLI/前端展示）。

设计：后端在 Provider.chat 和 AgentLoop 采集，前端（rich 命令行 / streamlit）只做展示。
分工：
- `UsageTracker`：一个进程内累计器，记录 调用次数 / prompt_tokens / completion_tokens /
  总耗时 / 估算成本。被 Provider.chat（采集单次）和 AgentLoop（聚合每次调查）共用。
- 成本估算用一张"按供应商+模型"的价格表（$/1M tokens），查不到就给出 0 或近似值，
  并标注 estimate=true，不冒充精确账单。

安全：不读取、不存储任何密钥；只统计用量数字。
"""
from __future__ import annotations

from dataclasses import dataclass

# 价格表：$ 每 1M tokens（输入/输出），近似值（公开定价，随供应商可调整）
# key: f"{vendor_id}/{model}"；模型名做前缀匹配
_PRICES: dict[str, list[float]] = {
    "deepseek/deepseek-chat": [0.27, 1.10],    # 2024 定价
    "openai/gpt-4o-mini": [0.15, 0.60],
    "openai/gpt-4o": [2.50, 10.00],
    "zhipu/glm-4-flash": [0.0, 0.0],           # 免费档
}


def lookup_price(vendor_id: str, model: str) -> list[float] | None:
    """按供应商+模型查 (输入$每1M, 输出$每1M)；没匹配到返回 None。"""
    key = f"{vendor_id}/{model}"
    if key in _PRICES:
        return _PRICES[key]
    # 前缀匹配（模型版本后缀，如 deepseek-chat-2025）
    prefix = f"{vendor_id}/"
    for k, v in _PRICES.items():
        if k.startswith(prefix) and model.startswith(k[len(prefix):]):
            return v
    return None


@dataclass
class CallStat:
    """一次模型调用的统计。"""
    model: str
    vendor_id: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: float = 0.0
    kind: str = "text"          # text | tool_calls

    # 成本估算（$）
    def cost_usd(self) -> float:
        price = lookup_price(self.vendor_id, self.model)
        if price is None:
            return 0.0
        return (self.prompt_tokens / 1e6) * price[0] + \
               (self.completion_tokens / 1e6) * price[1]

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "duration_ms": round(self.duration_ms, 1),
            "kind": self.kind,
            "cost_usd": round(self.cost_usd(), 6),
        }


class UsageTracker:
    """进程内用量累计器：跨多次调用累加 token / 耗时 / 成本。"""

    def __init__(self):
        self.calls: list[CallStat] = []

    def record(self, stat: CallStat) -> None:
        self.calls.append(stat)

    # ---- 聚合 ----
    @property
    def total_prompt_tokens(self) -> int:
        return sum(c.prompt_tokens for c in self.calls)

    @property
    def total_completion_tokens(self) -> int:
        return sum(c.completion_tokens for c in self.calls)

    @property
    def total_tokens(self) -> int:
        return self.total_prompt_tokens + self.total_completion_tokens

    @property
    def total_duration_ms(self) -> float:
        return sum(c.duration_ms for c in self.calls)

    @property
    def total_cost_usd(self) -> float:
        return sum(c.cost_usd() for c in self.calls)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def summary(self) -> dict:
        """给前端展示的汇总（JSON 友好）。"""
        priced = [c for c in self.calls if lookup_price(c.vendor_id, c.model) is not None]
        return {
            "call_count": self.call_count,
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
            "duration_ms": round(self.total_duration_ms, 1),
            "cost_usd": round(self.total_cost_usd, 6),
            "cost_estimated": len(priced) < self.call_count,
            "per_call": [c.to_dict() for c in self.calls],
        }


def fmt_cost(cost_usd: float) -> str:
    """人类可读的成本字符串（元/美元）。"""
    if cost_usd > 0.01:
        return f"${cost_usd:.4f}"
    if cost_usd > 0:
        return f"${cost_usd:.6f}"
    return "$0 (未定价)"
