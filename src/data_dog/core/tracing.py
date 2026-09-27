"""W3C Trace Context 传播（correlation-id，**不是**完整分布式追踪）。

边界（务必诚实理解）
--------------------
本模块做的是 **correlation-id 传播**：透传/生成 W3C `traceparent` 头，把
`trace_id` 注入日志，使**跨服务的一条请求链路**能用同一个 id 串起来。它
**不是** OpenTelemetry：不上报 span、不采样、不做 span 层级与耗时分解，也
没有导出到 Jaeger/Tempo。

为什么不上 OpenTelemetry SDK
----------------------------
本服务只有一个 HTTP 进程 + 一个数据库，真实的跨服务链路很少；引入 OTel SDK
会带来依赖、配置（collector 端点、采样率）与运行期成本，收益不成比例。
需要完整追踪时，可在 `traceparent` 这个标准接缝上外挂一个 OTel 处理器——
本模块只负责生成/解析，不锁定后续方案。

格式（W3C Trace Context Level 1）
---------------------------------
    traceparent = 00-<32 hex trace-id>-<16 hex span-id>-<2 hex flags>
客户端带的 trace-id 沿用（跨服务同一条链路），但 **span-id 每次新建**——
span-id 标识"本服务这次处理"，若照抄上游会污染链路。校验严格：字段长度、
十六进制、trace-id/span-id 非全零、版本号合法。
"""
from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

# 规范名（含连字符），HTTP 头不区分大小写
TRACEPARENT_HEADER = "traceparent"
_FLAGS_SAMPLED = "01"

# 严格校验：version(2) - trace-id(32) - span-id(16) - flags(2)，全小写十六进制。
# 只接受 version 00（Level 1 规定的当前版本）；更高版本按规范应忽略其尾部。
_TRACEPARENT_RE = re.compile(
    r"^(?P<ver>[0-9a-f]{2})-"
    r"(?P<trace>[0-9a-f]{32})-"
    r"(?P<span>[0-9a-f]{16})-"
    r"(?P<flags>[0-9a-f]{2})$"
)

_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16

# 当前请求的 trace 上下文（用 ContextVar 适配异步/多线程，与 request_id 同构）。
_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


def _new_trace_id() -> str:
    return uuid.uuid4().hex          # 128-bit → 32 hex


def _new_span_id() -> str:
    return uuid.uuid4().hex[:16]     # 64-bit → 16 hex


def parse_traceparent(header: str | None) -> tuple[str, str] | None:
    """解析 `traceparent`，返回 (trace_id, parent_span_id)；不合法 → None。

    不合法即按"没有上游 trace"处理（生成新的），不抛异常——追踪头损坏不该
    影响业务请求。
    """
    if not header:
        return None
    m = _TRACEPARENT_RE.match(header.strip().lower())
    if not m:
        return None
    trace_id, span_id = m.group("trace"), m.group("span")
    # 全零的 trace-id/span-id 是非法值（规范明确禁止）
    if trace_id == _ZERO_TRACE or span_id == _ZERO_SPAN:
        return None
    return trace_id, span_id


def start_trace(header: str | None) -> tuple[str, str]:
    """为一次入站请求建立追踪上下文，返回 (trace_id, traceparent 响应头值)。

    - 上游带了合法 `traceparent` → 沿用其 trace-id（链路对齐）；
    - 否则生成一个新的 trace-id。
    - 两种情况都**新建 span-id**（本服务的这次处理），并把 trace-id 注入
      ContextVar，供该请求内的所有日志行带出。
    """
    parsed = parse_traceparent(header)
    trace_id = parsed[0] if parsed else _new_trace_id()
    span_id = _new_span_id()
    _trace_id.set(trace_id)
    return trace_id, format_traceparent(trace_id, span_id)


def format_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    flags = _FLAGS_SAMPLED if sampled else "00"
    return f"00-{trace_id}-{span_id}-{flags}"


def get_trace_id() -> str:
    return _trace_id.get()


def clear_trace() -> None:
    _trace_id.set("")
