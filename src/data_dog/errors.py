# -*- coding: utf-8 -*-
"""统一失败语义（对齐架构 13.4）。

工具/Agent 层所有失败都收敛到有限几种错误码，而不是散落的字符串。
这样上层（Agent/日志/评估）可以稳定地对失败做分类处理。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict


class ToolErrorCode(str, Enum):
    INVALID_INPUT = "invalid_input"          # 入参缺失/类型错误
    DENIED = "denied"                        # 权限不足（越权）
    NOT_FOUND = "not_found"                  # 数据不存在
    AMBIGUOUS = "ambiguous"                  # 匹配不唯一，不能猜测
    VERSION_CONFLICT = "version_conflict"    # 资源版本与读取时不符
    CONTRACT_MISMATCH = "contract_mismatch"  # 契约/协议不符
    TIMEOUT = "timeout"                      # 超时
    UNAVAILABLE = "unavailable"              # 依赖服务不可用
    RATE_LIMITED = "rate_limited"            # 被限流
    EXECUTION_FAILED = "execution_failed"    # 执行失败（兜底）


def err(code: ToolErrorCode, detail: Any = None) -> Dict[str, Any]:
    """构造统一错误结果。"""
    d: Dict[str, Any] = {"ok": False, "error": code.value}
    if detail is not None:
        d["detail"] = detail
    return d


def ok(data: Dict[str, Any]) -> Dict[str, Any]:
    """构造统一成功结果（只读查询返回时包装）。"""
    return {"ok": True, **data}
