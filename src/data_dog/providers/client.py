"""供应商客户端：统一封装各家 LLM 的 chat 调用。

所有供应商都通过 OpenAI 兼容协议访问，因此这里只有一个实现。
新增供应商只需在 registry 里加一行，不用动这里。

超时与重试（都显式，不留给 SDK 默认）
------------------------------------
- 超时：单次 `chat()` 传 `timeout`（`DD_LLM_TIMEOUT_S`，默认 60s）；上游挂起必须能退出。
- 重试：客户端 `max_retries=0`——**一次 `chat()` 就是一次 HTTP 往返**。SDK 默认会
  自己重试 2 次且**没有墙钟上限**，最坏 ≈3×timeout，而调用方的总预算
  （`service/app.py` 的 `_DeadlineProvider`，默认 90s）只能在**调用前**检查，
  拦不住正在进行的那几次重试。重试预算应由"知道剩余预算"的上层显式管理。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from ..core.redact import redact
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


def _default_timeout() -> float | None:
    from .. import settings
    return settings.llm_timeout_s()


@dataclass
class LLMChatMessage:
    role: str          # "system" | "user" | "assistant" | "tool"
    content: str
    name: str | None = None   # tool 调用名（用于把工具结果喂回模型）
    tool_call_id: str | None = None
    tool_calls: list | None = None  # 仅 assistant 回显用：模型先前发出的原始 tool_calls
    # 视觉证据（VISTA-1 式无损视觉通道）：每项 {"mime_type": "image/png",
    # "data_base64": "..."}。**只对多模态 provider 有效**；纯文本 provider 绝不能带它
    # （OpenAI 兼容接口会因未知 content 形态报错）。默认 None = 纯文本（零变化）。
    images: list[dict[str, str]] | None = None


@dataclass
class LLMToolParam:
    """描述一个可被模型调用的函数式工具（OpenAI tool 定义）。"""
    name: str
    description: str
    parameters: dict   # JSON Schema 形态


class ChatProvider(Protocol):
    """`AgentLoop` 依赖的最小 chat 接口（结构化类型，非名义继承）。

    之所以用 Protocol 而不是要求各实现继承 `Provider`：本仓有多套**确定性
    provider**（offline_demo / synth_eval / mujoco_eval / e2e.replay），
    它们刻意不继承以保证离线可复现、零 SDK 依赖；但它们都实现了同形的
    `chat(...)`。用 Protocol 把这份"鸭子契约"显式化——既能被 mypy 校验，
    又不强迫它们建立继承关系。
    """

    def chat(
        self,
        messages: list[LLMChatMessage],
        *,
        tools: list[LLMToolParam] | None = ...,
        model: str | None = ...,
        temperature: float = ...,
        max_tokens: int = ...,
    ) -> dict: ...


class Provider:
    """一个具体的 LLM 供应商客户端。"""

    def __init__(self, vendor: VendorSpec, api_key: str,
                 timeout: float | None = None):
        self.vendor = vendor
        self._api_key = api_key
        # 单次请求超时：上游挂起时必须能退出，否则永久占用 worker。
        # 显式传参优先；否则读 DD_LLM_TIMEOUT_S；默认 60s。
        self.timeout = _default_timeout() if timeout is None else timeout
        # 统一用 OpenAI SDK，仅替换 base_url。
        #
        # `max_retries=0`：**不让 SDK 自己重试**，把重试预算收回我们自己的 deadline 里。
        # 为什么必须显式写出来（而不是用 SDK 默认的 2 次）：SDK 的重试是隐式的——
        # 有指数退避+抖动，但**没有墙钟上限**，而它重试的是"整次 HTTP 往返"，
        # 于是最坏耗时 ≈ (1+2)×timeout（默认 60s → 约 180s）。这与本仓的调用预算
        # 直接冲突：`_DeadlineProvider`（service/app.py）只在**每次调用之前**检查
        # 90s 总预算，无法中断 SDK 正在进行的那几次重试——结果是"预算说 90s、
        # 实际能跑 3 分钟"。设 0 之后，一次 `chat()` = 一次 HTTP 往返，
        # 超时与重试都由我们的预算与上层失败关闭（`LLMCallError`）决定，行为可预测、
        # 可测、可配。真要重试，也应该由上层在"知道剩余预算"的前提下显式做。
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key, base_url=vendor.base_url,
                              max_retries=0)

    @classmethod
    def from_config(cls, vendor_id: str, api_key: str,
                    timeout: float | None = None, base_url: str = "") -> Provider:
        """按供应商 id 构造。@@base_url@@ 非空时**覆盖**内置端点的 base_url。

        用于"自带模型/网关"（@@DD_LLM_VENDOR=custom@@）：端点来自配置而非代码，
        @@.env@@ 里能读到就显式传，读到不就读 @@settings@@（见 registry._custom_vendor）。
        """
        spec = get_vendor(vendor_id)
        if base_url:
            spec = replace(spec, base_url=base_url)
        if not spec.base_url:
            raise ValueError(
                f"供应商 {vendor_id!r} 缺 base_url：自定义端点请设 DD_LLM_BASE_URL")
        return cls(spec, api_key, timeout=timeout)

    def chat(
        self,
        messages: list[LLMChatMessage],
        *,
        tools: list[LLMToolParam] | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        usage_tracker=None,
    ) -> dict:
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
            # ⚠️ 脱敏**在源头做**：SDK 异常消息可能带 Authorization / api_key / 大块
            # base64（多模态请求体）。这段 detail 会流到 HTTP 错误体与调查报告，故
            # 先按真实密钥值 + 常见密钥形态抹掉，再截断（见 core/redact.py）。
            raise LLMCallError(
                ToolErrorCode.UNAVAILABLE,
                redact(f"{type(e).__name__}: {e}", secrets=[self._api_key])) from e
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

    def ping(self, model: str | None = None) -> bool:
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
    content: object = m.content
    if m.images:
        # OpenAI 兼容的"内容分片"：文本 + 内联图片（data URL）。
        # VISTA-1 的口径：**原始帧直接送模型**，不做有损描述/压缩表征。
        parts: list[dict] = [{"type": "text", "text": m.content or ""}]
        for img in m.images:
            mime = img.get("mime_type") or "image/png"
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{img.get('data_base64', '')}"},
            })
        content = parts
    d: dict = {"role": m.role, "content": content}
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
