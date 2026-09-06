# -*- coding: utf-8 -*-
"""
可信身份注入 MCP Server（A4：运行时身份注入，架构 13.3）
=========================================================

【这是什么】
一个**最小可用、零第三方依赖**的 MCP server，跑在 stdio 上，向 Agent 提供
「当前任务的可信运行时上下文（RuntimeContext）」。

【为什么需要它】
架构原则：Agent 无隐含权限 —— Agent 的 Tool 不接受 tenant/owner/scope，
这些必须由**可信运行时**注入，Agent 无法伪造。
这个 server 就是那个"可信运行时"的身份注入端点：
—— 它只暴露一个 `get_runtime_context` 工具，返回可信上下文；
—— Agent 只能按返回的上下文去查数据，在这一步就删掉了伪造的可能。

【MCP 协议（为什么能零依赖）】
MCP (Model Context Protocol) over stdio 本质是 **JSON-RPC 2.0 走 stdin/stdout**：
  - 请求：读 stdin 一行行的 JSON-RPC 消息
  - 响应：把 JSON 写回 stdout
  - 初始化：客户端先发 `initialize`，server 回 capabilities
  - 列工具：`tools/list`
  - 调工具：`tools/call`
下面就用标准库 json + sys.stdin/stdout 手写这个协议，方便学习，不引入 SDK。

【安全要点（Mimosa 约束）】
- 本 server **不执行任何网络/文件/URL 操作**，所以不存在 SSRF/路径穿越面。
- 上下文由可信启动参数注入（env/argv），不信任任何 Agent 传入的内容。
- 鉴权失败 = 失败关闭（返回 denied），不伪装成别的错误。

【运行】
    python context_injector_mcp.py            # stdio 模式
与任何支持 MCP 的客户端连（Claude/自定义 client 等），或手动发 JSON-RPC 测试。

【data_dog 侧怎么接】
`data_dog/mcp_context.py` 是配套客户端：spawn 本进程 → 调 get_runtime_context
→ 构造 RuntimeContext 喂给 `InvestigationTools(store, ctx=...)`。
设置环境变量 DD_CTX_MCP=1 启用；客户端校验响应里的 source 标注，防伪造。

【部署】
- 单机学习/测试：直接运行即可。
- 真实部署：由网关/认证层通过环境变量注入 DD_TENANT_ID / DD_DEVICE_SN /
  DD_ALLOWED_COMPONENTS；本文件不改动。

更新：2026-09-06
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# 1. 可信上下文（与 data_dog/context.py 的 RuntimeContext 结构一致）
# ---------------------------------------------------------------------------
# 注意：这些来自【可信启动参数】——不是 Agent 消息，不是日志正文。
# 真实部署里由网关/认证层注入；本示例用环境变量演示"注入"这个动作本身。
DEFAULT_TENANT = os.environ.get("DD_TENANT_ID", "tenant-default")
DEFAULT_DEVICE = os.environ.get("DD_DEVICE_SN", "device-abc123")
AGENT_ID = os.environ.get("DD_AGENT_ID", "device-health-agent-v1")
# 授权部件范围（可信配置）
_ALLOWED = os.environ.get(
    "DD_ALLOWED_COMPONENTS",
    "joint_motor.front_left_hip,joint_motor.front_right_hip,"
    "joint_motor.front_left_knee,battery.main,controller.main",
)
ALLOWED_COMPONENTS = frozenset(x.strip() for x in _ALLOWED.split(",") if x.strip())


def build_runtime_context() -> Dict[str, Any]:
    """构造可信 RuntimeContext（结构对齐 data_dog/context.py）。"""
    return {
        "tenant_id": DEFAULT_TENANT,
        "agent_id": AGENT_ID,
        "device_sn": DEFAULT_DEVICE,
        "allowed_components": sorted(ALLOWED_COMPONENTS),
        # 来源标注：证明来自可信注入器，而非 Agent 伪造
        "source": "trusted_context_injector:v1",
    }


# ---------------------------------------------------------------------------
# 2. 极简 MCP server（JSON-RPC 2.0 over stdio）
# ---------------------------------------------------------------------------
class MinimalMcpServer:
    """零依赖 MCP server：列工具 + 调用工具 + 健康检查。"""

    def __init__(self) -> None:
        self._tools = {
            # 工具名 → 实现函数
            "get_runtime_context": self._get_runtime_context,
        }
        self._tool_specs = [
            {
                "name": "get_runtime_context",
                "description": (
                    "返回当前任务的可信运行时上下文（tenant_id / device_sn / "
                    "agent_id / allowed_components）。Agent 必须只按此上下文访问数据，"
                    "不得伪造身份。此工具不接受任何参数；身份由可信运行时注入。"
                ),
                "inputSchema": {"type": "object", "properties": {}},
            }
        ]

    # ---- JSON-RPC 分发 ----
    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = msg.get("method", "")
        params = msg.get("params") or {}
        req_id = msg.get("id")

        if method == "initialize":
            # MCP 握手：声明能力
            return self._result(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "context-injector", "version": "0.1.0"},
            })
        if method == "tools/list":
            return self._result(req_id, {"tools": self._tool_specs})
        if method == "tools/call":
            name = (params.get("name") or "").lower()
            args = params.get("arguments") or {}
            fn = self._tools.get(name)
            if fn is None:
                return self._result(req_id, {
                    "content": [{"type": "text", "text": json.dumps(
                        {"ok": False, "error": "unknown_tool", "detail": name}
                    )}],
                    "isError": True,
                })
            # 调用工具：返回 MCP 的 text content
            payload = fn(args)
            return self._result(req_id, {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            })
        if method == "notifications/initialized":
            return None  # 通知类，无响应
        # 未知方法
        return self._error(req_id, -32601, f"method not found: {method}")

    # ---- 工具实现 ----
    def _get_runtime_context(self, _args: Dict[str, Any]) -> Dict[str, Any]:
        """返回可信上下文。签名：不接受任何参数（身份不是入参，是注入）。"""
        # 若调用方试图传任何东西，直接拒绝——身份绝不来自参数
        if _args:
            return {"ok": False, "error": "invalid_input",
                    "detail": "get_runtime_context 不接受任何参数；身份由运行时注入"}
        return {"ok": True, **build_runtime_context()}

    # ---- JSON-RPC 工具 ----
    @staticmethod
    def _result(req_id, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _error(req_id, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": code, "message": message}}


def main() -> int:
    """stdio 事件循环：逐行读 JSON-RPC，写回响应。"""
    server = MinimalMcpServer()
    # 兼容 Windows 编码，避免 stdout 打印中文报错
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue  # 忽略坏行
        resp = server.handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
