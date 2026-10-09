"""A4 配套客户端：从自研 context-injector MCP 获取可信运行时上下文。

连接策略：**常驻复用**（spare-spawn-per-request 的升级版）。
- 第一次 fetch 时 spawn server 并完成 MCP 握手，之后整个进程生命周期内复用；
- server 进程意外死亡 / server 路径变更 → 自动关旧建新；
- 单次请求失败 → 关掉当前进程、全新 spawn 重试一次；再失败 → 抛 McpContextError。

预算（次数 + 墙钟 + 抖动退避，与 `state/redis.py` 的 mutate 同一口径）
--------------------------------------------------------------------
- **看门狗由 `timeout_s` 派生**：`_watchdog_s(timeout_s) = timeout_s + 余量`，
  保证"看门狗 ≥ 本次调用超时"这条不变量——否则会把"慢"误判成"死"，在超时之前
  先把还在正常工作的 server 杀掉。
- **重试一次**，但两次之间是**抖动退避**（错误刚发生时立刻重连=对着还没恢复的
  server 连打，多副本还会同刻一起打）。
- **总预算** `_RETRY_TOTAL_BUDGET_S` 封住整次 `fetch_trusted_context()`：
  每次尝试的超时被夹到剩余预算内（`_attempt_timeout_s`），预算不够就不再发起
  下一次尝试——不会出现"重试一次 × 每次 20s"把预算悄悄翻倍的情况。

安全语义（不变）：
- 校验响应里的 source 标注（防伪装 server）；
- 任何失败都向上抛，由 AgentLoop **失败关闭**，绝不降级为无鉴权。
"""
from __future__ import annotations

import atexit
import json
import logging
import random
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import settings
from .context import RuntimeContext

_log = logging.getLogger(__name__)

EXPECTED_SOURCE = "trusted_context_injector:v1"
SERVER_NAME = "context_injector_mcp.py"
REQUEST_TIMEOUT_S = 20.0

# 看门狗余量：看门狗必须**严格晚于**本次调用超时（见模块 docstring 的不变量）。
# 0.5s 足够覆盖线程调度抖动，又不至于让"超时"这件事被推迟太多。
_WATCHDOG_SLACK_S = 0.5
# 整次 fetch_trusted_context（含最多两次尝试与退避）的总预算。默认 30s：
# 与 REQUEST_TIMEOUT_S=20s 同量级——够"死进程重建 + 一次真机会"，
# 又明显小于调查端点的 90s 总预算，不至于把上下文获取变成调查的主要耗时。
_RETRY_TOTAL_BUDGET_S = 30.0
# 尝试次数上限：沿用既有语义（失败后"关旧建新"重试一次；再失败即失败关闭）。
_MAX_ATTEMPTS = 2
# 两次尝试之间的抖动退避基数（指数不必要：最多两次尝试）。它消耗同一个总预算，
# 故取值很小——目的是"错开同刻重连"，不是"等下游恢复"。
_RETRY_BASE_SLEEP_S = 0.25


class McpContextError(RuntimeError):
    """可信上下文获取失败（server 不可用 / 响应非法 / 来源校验不过）。"""


def _watchdog_s(timeout_s: float, *, slack_s: float = _WATCHDOG_SLACK_S) -> float:
    """看门狗时长：**≥ 调用超时**（+余量）。不变量见模块 docstring。"""
    return max(0.0, float(timeout_s)) + max(0.0, float(slack_s))


def _attempt_timeout_s(timeout_s: float, remaining_budget_s: float) -> float:
    """单次尝试的超时：不超过剩余总预算（≤0 表示预算已尽，别再发起尝试）。"""
    return max(0.0, min(max(0.0, float(timeout_s)), float(remaining_budget_s)))


def _retry_delay_s(attempt: int, *,
                   rand: Callable[[], float] = random.random) -> float:
    """第 `attempt` 次尝试失败后的退避秒数（抖动 `U(0.5, 1.5)`）。"""
    nominal = _RETRY_BASE_SLEEP_S * (2 ** max(0, int(attempt) - 1))
    return nominal * (0.5 + rand())


def _default_server_path() -> Path:
    """项目内运行时副本：<project>/mcp_servers/context_injector_mcp.py。"""
    return Path(__file__).resolve().parents[2] / "mcp_servers" / SERVER_NAME


def _server_path() -> Path:
    """允许用 DD_CTX_MCP_SERVER 覆盖 server 路径（如本地备用副本）。"""
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
    def _start(self, timeout_s: float = REQUEST_TIMEOUT_S) -> None:
        """spawn server 并完成 MCP 握手（initialize → notifications/initialized）。

        `timeout_s` 由调用方传入（`fetch`）：握手也是"一次调用"，必须与请求共享
        同一个预算视角——固定用 `REQUEST_TIMEOUT_S` 会让"把超时调小"失效
        （握手仍能占用 20s）。
        """
        self.proc = subprocess.Popen(
            [sys.executable, str(self.server_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8",
        )
        result = self._request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "data_dog", "version": "0.1.0"},
        }, timeout_s=timeout_s)
        name = (result.get("serverInfo") or {}).get("name", "")
        if name != "context-injector":
            raise McpContextError(f"非预期的 server: {name!r}")
        self._notify("notifications/initialized")

    def fetch(self, timeout_s: float = REQUEST_TIMEOUT_S) -> RuntimeContext:
        """取一次可信上下文（复用常驻进程；进程死了先重建）。"""
        if not self.is_alive():
            self.close()
            self._start(timeout_s)
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
        """发一个带看门狗超时的请求（超时杀进程 → 后续必然失败关闭）。

        看门狗 = `_watchdog_s(timeout_s)`（**≥** 本次调用超时，见该函数与模块
        docstring 的不变量）。`_recv` 的 readline 本身没有超时参数，看门狗就是
        唯一的硬上界，故"晚于调用超时"这条必须成立。
        """
        req_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        watchdog = threading.Timer(_watchdog_s(timeout_s), self._kill)
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
    """取可信上下文：优先复用常驻 server，坏了自动重建，重试一次后仍失败则抛错。

    预算口径（详见模块 docstring）：
    - 总预算 `_RETRY_TOTAL_BUDGET_S` 覆盖**整次调用**（含两次尝试与退避）；
      每次尝试的超时 = `_attempt_timeout_s(timeout_s, 剩余预算)`；
    - 两次尝试之间是**抖动退避**（立即重连等于对着还没恢复的 server 连打）；
    - 预算不足以再发起一次尝试 → 直接收尾抛错（不"再试一次看看"，那会让
      最坏耗时随重试次数线性放大）。

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

    deadline = time.monotonic() + max(0.0, _RETRY_TOTAL_BUDGET_S)
    last_error: Exception | None = None
    attempts = 0
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if attempt > 1:
            delay = _retry_delay_s(attempt - 1)
            if delay >= deadline - time.monotonic():
                break                       # 退避都放不下 → 预算已尽，别再试
            time.sleep(delay)
            # 常驻进程可能已坏：丢弃，下一次尝试全新 spawn
            if client is not None:
                client.close()
            client = None
        budget = _attempt_timeout_s(timeout_s, deadline - time.monotonic())
        if budget <= 0.0:
            break                           # 预算耗尽：不发起（也不假装试过）
        attempts = attempt
        try:
            if client is None:
                client = ContextInjectorClient(path)
            ctx = client.fetch(budget)
        except Exception as e:              # noqa: BLE001 - 需归一化后再上抛
            last_error = e
            _log.warning("可信上下文获取失败", extra={
                "attempt": attempt, "budget_s": round(budget, 3),
                "error": f"{type(e).__name__}: {e}"})
            if client is not None:
                client.close()
            client = None
            continue
        _PERSISTENT = client
        return ctx

    # 全部尝试失败/预算耗尽 → 失败关闭（常驻单例置空，避免留着一个已知坏掉的连接）
    if client is not None:
        client.close()
    _PERSISTENT = None
    raise McpContextError(
        f"可信上下文获取失败（尝试 {attempts} 次，总预算 {_RETRY_TOTAL_BUDGET_S}s）: "
        f"{last_error if last_error is not None else '预算不足以发起任何尝试'}") \
        from last_error
