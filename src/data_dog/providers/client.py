# -*- coding: utf-8 -*-
"""供应商客户端：统一封装各家 LLM 的 chat 调用。

所有供应商都通过 OpenAI 兼容协议访问，因此这里只有一个实现。
新增供应商只需在 registry 里加一行，不用动这里。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..errors import ToolErrorCode
from .registry import VendorSpec, get_vendor


class LLMCallError(RuntimeError):
    """LLM 调用失败（已归一化为统一错误码）。

    调用方可用 `e.code` 稳定分类（timeout / unavailable），无需认识各 SDK
    的异常类型。
    """

    def __init__(self, code: ToolErrorCode, detail: str = ""):
        super().__init__(detail or code.value)
        self.code = code
        self.detail = detail


def _is_timeout(e: BaseException) -> bool:
    """判断异常是否为超时（跨 SDK/底层库，按类型名与内建超时识别）。"""
    if isinstance(e, (TimeoutError,)):
        return True
    for cls in type(e).__mro__:
        if cls.__name__ in ("APITimeoutError", "ConnectTimeout", "ReadTimeout",
                            "Timeout", "TimeoutException"):
            return True
    # 有些库把超时包在 cause 里
    cause = getattr(e, "__cause__", None)
    if cause is not None and cause is not e:
        return _is_timeout(cause)
    return False

# 默认超时（秒）。上游挂起时必须能超时退出，否则会永久占用 worker。
# 可用 DD_LLM_TIMEOUT_S 覆盖；设为 0 或负值表示不限时（不推荐）。
_DEFAULT_TIMEOUT_S = 60.0


def _default_timeout() -> Optional[float]:
    raw = os.environ.get("DD_LLM_TIMEOUT_S", "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_S
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_S
    return v if v > 0 else None


@dataclass
class LLMChatMessage:
    role: str          # "system" | "user" | "assistant" | "tool"
    content: str
    name: Optional[str] = None   # tool 调用名（用于把工具结果喂回模型）
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list] = None  # 仅 assistant 回显用：模型先前发出的原始 tool_calls


@dataclass
class LLMToolParam:
    """描述一个可被模型调用的函数式工具（OpenAI tool 定义）。"""
    name: str
    description: str
    parameters: dict   # JSON Schema 形态


class Provider:
    """一个具体的 LLM 供应商客户端。"""

    def __init__(self, vendor: VendorSpec, api_key: str,
                 timeout: Optional[float] = None):
        self.vendor = vendor
        self._api_key = api_key
        # 单次请求超时：上游挂起时必须能退出，否则永久占用 worker。
        # 显式传参优先；否则读 DD_LLM_TIMEOUT_S；默认 60s。
        self.timeout = _default_timeout() if timeout is None else timeout
        # 统一用 OpenAI SDK，仅替换 base_url
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key, base_url=vendor.base_url)

    @classmethod
    def from_config(cls, vendor_id: str, api_key: str) -> "Provider":
        return cls(get_vendor(vendor_id), api_key)

    def chat(
        self,
        messages: List[LLMChatMessage],
        *,
        tools: Optional[List[LLMToolParam]] = None,
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        usage_tracker=None,
    ) -> Dict:
        """调一次 chat；若传了 tools 且模型决定调工具，返回含 tool_calls 的结果。

        usage_tracker：可选，传入 UsageTracker 时记录本次调用的 token / 耗时（可观测性）。
        """
        model = model or self.vendor.default_model
        api_messages = [_to_api(m) for m in messages]
        kwargs: dict = {
            "model": model,
            "messages": api_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools and len(tools) > 0:
            kwargs["tools"] = [_to_api_tool(t) for t in tools]
            kwargs["tool_choice"] = "auto"

        import time
        if self.timeout is not None:
            kwargs["timeout"] = self.timeout
        t0 = time.perf_counter()
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as e:                          # noqa: BLE001
            # 归一化为统一失败语义：超时 → timeout；其余 → unavailable。
            # 调用方（AgentLoop）据此稳定分类，不必认识各 SDK 的异常类型。
            if _is_timeout(e):
                raise LLMCallError(ToolErrorCode.TIMEOUT,
                                   f"LLM 调用超时（{self.timeout}s）") from e
            raise LLMCallError(ToolErrorCode.UNAVAILABLE,
                               f"{type(e).__name__}: {e}") from e
        duration_ms = (time.perf_counter() - t0) * 1000.0
        choice = resp.choices[0]
        msg = choice.message

        # ---- 可观测性：采集 token / 耗时，回填进返回 dict（backward compatible）----
        usage = getattr(resp, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", 0) if usage is not None else 0
        completion_tokens = getattr(usage, "completion_tokens", 0) if usage is not None else 0
        if usage_tracker is not None:
            from ..observability import CallStat
            kind = "tool_calls" if getattr(msg, "tool_calls", None) else "text"
            usage_tracker.record(CallStat(
                model=model,
                vendor_id=self.vendor.id,
                prompt_tokens=int(prompt_tokens or 0),
                completion_tokens=int(completion_tokens or 0),
                duration_ms=duration_ms,
                kind=kind,
            ))

        # 若模型想调工具，返回工具调用候选；否则返回文本
        if getattr(msg, "tool_calls", None):
            return {
                "kind": "tool_calls",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,  # 仍为 JSON 字符串
                    }
                    for tc in msg.tool_calls
                ],
                "content": msg.content or "",
                "__raw_tool_calls": [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
                "__usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "duration_ms": round(duration_ms, 1),
                },
            }
        return {"kind": "text", "content": msg.content or "",
                "finish_reason": choice.finish_reason,
                "__usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "duration_ms": round(duration_ms, 1),
                }}

    def ping(self, model: Optional[str] = None) -> bool:
        """连通性/凭据校验，供交互式接入时使用。"""
        try:
            self.chat(
                [LLMChatMessage(role="user", content="ping")],
                model=model,
                max_tokens=4,
            )
            return True
        except Exception:
            return False


def _to_api(m: LLMChatMessage) -> dict:
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    d = {"role": m.role, "content": m.content}
    if m.tool_calls is not None:
        # 回显模型先前发出的 tool_calls，协议要求保留
        d["tool_calls"] = m.tool_calls
    return d


def _to_api_tool(t: LLMToolParam) -> dict:
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        },
    }
