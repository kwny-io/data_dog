# -*- coding: utf-8 -*-
"""运行时可信身份 / 授权上下文（对齐架构 13.3）。

原则：Agent Tool 不接受 tenant / owner / scope（它可控输入里没有这些），
而是由**可信运行时**在发起任务时注入一个 RuntimeContext。Agent 传的
device/component 只是"请求"，真正生效的访问范围以 ctx 为准——会被校验并收紧。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet


@dataclass(frozen=True)
class RuntimeContext:
    """一次 Agent 任务的可信上下文（由运行时注入，Agent 无法伪造）。"""
    tenant_id: str
    agent_id: str
    device_sn: str                      # 可信的当前设备
    allowed_components: FrozenSet[str] = field(default_factory=frozenset)  # 可用部件范围

    def can_access(self, device_sn: str, component: str) -> bool:
        """运行时唯一鉴权判断：设备必须匹配，部件必须在允许范围内。"""
        if device_sn != self.device_sn:
            return False                 # 设备不属于当前上下文
        if self.allowed_components and component not in self.allowed_components:
            return False                 # 部件超出授权范围
        return True


def default_context() -> RuntimeContext:
    """单机场景的默认可信上下文：白名单即可用部件。"""
    from .evidence.store import ALLOWED_COMPONENTS
    return RuntimeContext(
        tenant_id="tenant-default",
        agent_id="device-health-agent-v1",
        device_sn="device-abc123",
        allowed_components=frozenset(ALLOWED_COMPONENTS),
    )
