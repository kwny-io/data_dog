"""A4 配套客户端：从自研 context-injector MCP 获取可信运行时上下文。

连接策略：**常驻复用**（spare-spawn-per-request 的升级版）。
- 第一次 fetch 时 spawn server 并完成 MCP 握手，之后整个进程生命周期内复用；
- server 进程意外死亡 / server 路径变更 → 自动关旧建新；
- 单次请求失败 → 关掉当前进程、全新 spawn 重试一次；再失败 → 抛 McpContextError。

安全语义（不变）：
- 校验响应里的 source 标注（防伪装 server）；
- 任何失败都向上抛，由 AgentLoop **失败关闭**，绝不降级为无鉴权。
"""
from __future__ import annotations

import atexit
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from . import settings
from .context import RuntimeContext

EXPECTED_SOURCE = "trusted_context_injector:v1"
SERVER_NAME = "context_injector_mcp.py"
REQUEST_TIMEOUT_S = 20.0


class McpContextError(RuntimeError):
    """可信上下文获取失败（server 不可用 / 响应非法 / 来源校验不过）。"""


def _default_server_path() -> Path:
    """项目内运行时副本：<project>/mcp_servers/context_injector_mcp.py。"""
    return Path(__file__).resolve().parents[2] / "mcp_servers" / SERVER_NAME


def _server_path() -> Path:
    """允许用 DD_CTX_MCP_SERVER 覆盖 server 路径（如桌面学习副本）。"""
    override = settings.ctx_mcp_server()
    if override:
        return Path(override)
    return _default_server_path()


class ContextInjectorClient:
    """与一个 context-injector server 进程的常驻 stdio 连接。

    生命周期：_start()（spawn + MCP 握手）→ 多次 fetch() → close()。
    进程死后 is_alive() 为 False，下次 fetch 前会自动重建。
    """

    def __init__(self, server_path: Path):
        self.server_path = server_path
        self.proc: subprocess.Popen | None = None
        self._next_id = 1

    # ---- 生命周期 ----
    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self) -> None:
        """server 在 Windows 上 stdin EOF 后可能不退出，主动收尾。"""
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()
        self.proc = None

    def _kill(self) -> None:
        """看门狗触发：杀掉卡死的进程（is_alive 随即变 False）。"""
        if self.proc is not None:
            try:
                self.proc.kill()
            except Exception:
                pass

    # ---- MCP 会话 ----
    def _start(self) -> None:
        """spawn server 并完成 MCP 握手（initialize → notifications/initialized）。"""
        self.proc = subprocess.Popen(
            [sys.executable, str(self.server_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8",
        )
        result = self._request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "data_dog", "version": "0.1.0"},
        })
        name = (result.get("serverInfo") or {}).get("name", "")
        if name != "context-injector":
            raise McpContextError(f"非预期的 server: {name!r}")
        self._notify("notifications/initialized")

    def fetch(self, timeout_s: float = REQUEST_TIMEOUT_S) -> RuntimeContext:
        """取一次可信上下文（复用常驻进程；进程死了先重建）。"""
        if not self.is_alive():
            self.close()
            self._start()
        result = self._request("tools/call", {
            "name": "get_runtime_context", "arguments": {},
        }, timeout_s=timeout_s)
        if result.get("isError"):
            raise McpContextError(f"tools/call 失败: {result}")
        return _payload_to_context(_payload_of(result))

    # ---- JSON-RPC 基元 ----
    def _send(self, msg: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _recv(self, req_id: int) -> dict[str, Any]:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise McpContextError("server 无响应（提前退出或被超时终止）")
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            if resp.get("id") == req_id:
                if "error" in resp:
                    raise McpContextError(f"JSON-RPC 错误: {resp['error']}")
                return resp["result"]

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _request(
        self, method: str, params: dict[str, Any],
        timeout_s: float = REQUEST_TIMEOUT_S,
    ) -> dict[str, Any]:
        """发一个带看门狗超时的请求（超时杀进程 → 后续必然失败关闭）。"""
        req_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        watchdog = threading.Timer(timeout_s, self._kill)
        watchdog.daemon = True
        try:
            watchdog.start()
            return self._recv(req_id)
        finally:
            watchdog.cancel()


# ---- 响应解析与校验 ----
def _payload_of(result: dict[str, Any]) -> dict[str, Any]:
    texts = [c.get("text", "") for c in result.get("content", [])]
    try:
        return json.loads("".join(texts))
    except json.JSONDecodeError as e:
        raise McpContextError(f"响应不是合法 JSON: {e}") from None


def _payload_to_context(payload: dict[str, Any]) -> RuntimeContext:
    """校验 ok / source 标注 / 必填字段，构造 RuntimeContext。"""
    if payload.get("ok") is not True:
        raise McpContextError(f"server 返回失败: {payload}")
    if payload.get("source") != EXPECTED_SOURCE:
        raise McpContextError(
            f"来源校验失败: {payload.get('source')!r} != {EXPECTED_SOURCE!r}")
    tenant = payload.get("tenant_id")
    agent = payload.get("agent_id")
    device = payload.get("device_sn")
    comps = payload.get("allowed_components") or []
    if not (tenant and agent and device):
        raise McpContextError(f"上下文字段缺失: {payload}")
    return RuntimeContext(
        tenant_id=str(tenant), agent_id=str(agent), device_sn=str(device),
        allowed_components=frozenset(str(c) for c in comps),
    )


# ---- 常驻单例管理 ----
_PERSISTENT: ContextInjectorClient | None = None


def close_persistent_context_server() -> None:
    """显式关闭常驻 server（测试/退出时用；atexit 也会兜底调用）。"""
    global _PERSISTENT
    if _PERSISTENT is not None:
        _PERSISTENT.close()
        _PERSISTENT = None


atexit.register(close_persistent_context_server)


def fetch_trusted_context(timeout_s: float = REQUEST_TIMEOUT_S) -> RuntimeContext:
    """取可信上下文：优先复用常驻 server，坏了自动重建，再失败则抛错。

    注意：server 路径不存在时直接抛错，**不动**已有常驻连接。
    """
    path = _server_path()
    if not path.exists():
        raise McpContextError(f"server 不存在: {path}")

    global _PERSISTENT
    client = _PERSISTENT
    if client is not None and (client.server_path != path or not client.is_alive()):
        client.close()
        client = None

    try:
        if client is None:
            client = ContextInjectorClient(path)
        ctx = client.fetch(timeout_s)
    except Exception:
        # 常驻进程可能已坏：丢弃，全新 spawn 重试一次；再失败 → 抛（上层失败关闭）
        if client is not None:
            client.close()
        client = ContextInjectorClient(path)
        ctx = client.fetch(timeout_s)
    _PERSISTENT = client
    return ctx
