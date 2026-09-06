# -*- coding: utf-8 -*-
"""供应商客户端：统一封装各家 LLM 的 chat 调用。

所有供应商都通过 OpenAI 兼容协议访问，因此这里只有一个实现。
新增供应商只需在 registry 里加一行，不用动这里。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .registry import VendorSpec, get_vendor


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

    def __init__(self, vendor: VendorSpec, api_key: str):
        self.vendor = vendor
        self._api_key = api_key
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
    ) -> Dict:
        """调一次 chat；若传了 tools 且模型决定调工具，返回含 tool_calls 的结果。"""
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

        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message

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
            }
        return {"kind": "text", "content": msg.content or "",
                "finish_reason": choice.finish_reason}

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
