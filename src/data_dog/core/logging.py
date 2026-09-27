# -*- coding: utf-8 -*-
"""结构化日志（JSON）+ 请求上下文（request_id）。

设计取舍
--------
- **服务端用结构化日志，CLI/demo 继续用 print**：`cli.py`、`*_demo.py`、
  `simulate.py` 的输是给人读的终端文本（rich 渲染、进度、演示），改成 JSON
  反而更难读，且它们不是被日志系统采集的对象。本模块服务的是**长驻服务**
  （`service/`）——那里需要可被采集、可检索、可关联的日志。
- **JSON Lines**：每行一个 JSON 对象，直接可被 Loki/ELK/CloudWatch 消费。
- **request_id 注入**：每个请求有唯一 id，贯穿该请求内所有日志行，
  出事时能按 id 把一次请求的全部日志捞出来。
- **零外部依赖**：只用标准库 `logging` + `json`，不引 structlog/loguru。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any, Dict, Optional

# 当前请求上下文（request_id 等）。用 ContextVar 以适配异步/多线程。
_request_id: ContextVar[str] = ContextVar("request_id", default="")

# 标准 LogRecord 自带的属性，序列化时需排除，只保留 extra 里的业务字段
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}


def set_request_id(rid: Optional[str] = None) -> str:
    """设置当前上下文的 request_id（不传则生成一个）。返回最终值。"""
    rid = rid or uuid.uuid4().hex[:16]
    _request_id.set(rid)
    return rid


def get_request_id() -> str:
    return _request_id.get()


def clear_request_id() -> None:
    _request_id.set("")


class JsonFormatter(logging.Formatter):
    """把 LogRecord 格式化为单行 JSON。"""

    def __init__(self, service: str = "data-dog", version: str = ""):
        super().__init__()
        self.service = service
        self.version = version

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "service": self.service,
        }
        if self.version:
            payload["version"] = self.version
        rid = get_request_id()
        if rid:
            payload["request_id"] = rid

        # 业务字段（logger.info("x", extra={"foo": 1}) 里的 foo）
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                payload[k] = v

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(*, level: Optional[str] = None,
                  service: Optional[str] = None,
                  version: str = "",
                  json_output: Optional[bool] = None) -> None:
    """配置根 logger。幂等，可重复调用。

    - level：`DD_LOG_LEVEL`（默认 INFO）
    - json_output：`DD_LOG_JSON`（默认 1=JSON；设 0 用人类可读格式，便于本地调试）
    """
    lvl = (level or os.environ.get("DD_LOG_LEVEL", "INFO")).upper()
    svc = service or os.environ.get("DD_SERVICE_NAME", "data-dog")
    if json_output is None:
        json_output = os.environ.get("DD_LOG_JSON", "1").strip() != "0"

    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JsonFormatter(service=svc, version=version))
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))

    root = logging.getLogger()
    # 移除既有 handler（避免重复输出），再挂我们的
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(getattr(logging, lvl, logging.INFO))


def get_logger(name: str) -> logging.Logger:
    """取一个 logger（约定 `get_logger(__name__)`）。"""
    return logging.getLogger(name)
