# -*- coding: utf-8 -*-
"""横切关注点（cross-cutting concerns）：日志、请求上下文等。"""
from .logging import (
    clear_request_id,
    get_logger,
    get_request_id,
    set_request_id,
    setup_logging,
)

__all__ = [
    "setup_logging",
    "get_logger",
    "set_request_id",
    "get_request_id",
    "clear_request_id",
]
