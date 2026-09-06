# -*- coding: utf-8 -*-
"""供应商抽象层：注册表 + 客户端 + 配置。"""
from .registry import (
    BUILTIN_VENDORS,
    VendorSpec,
    get_vendor,
    vendor_ids,
)
from .client import (
    LLMChatMessage,
    LLMToolParam,
    Provider,
)
from . import config

__all__ = [
    "BUILTIN_VENDORS",
    "VendorSpec",
    "get_vendor",
    "vendor_ids",
    "LLMChatMessage",
    "LLMToolParam",
    "Provider",
    "config",
]
