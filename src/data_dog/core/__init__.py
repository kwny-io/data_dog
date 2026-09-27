"""横切关注点（cross-cutting concerns）：日志、请求上下文、追踪等。"""
from .logging import (
    clear_request_id,
    get_logger,
    get_request_id,
    set_request_id,
    setup_logging,
)
from .tracing import (
    clear_trace,
    format_traceparent,
    get_trace_id,
    parse_traceparent,
    start_trace,
)

__all__ = [
    "setup_logging",
    "get_logger",
    "set_request_id",
    "get_request_id",
    "clear_request_id",
    "start_trace",
    "get_trace_id",
    "clear_trace",
    "parse_traceparent",
    "format_traceparent",
]
