"""算子沙箱运行时（P4-1）——两级纵深防御。

目标（docs/p4/01_sandbox.md §1）：即使受限 DSL 的解释器存在漏洞，**被绕过的算子
也不伤宿主**——不读宿主文件、不联网、不耗尽 CPU/内存、不挂死进程。

分层
----
  Tier 0  InlineExecutor     默认，单进程。仅加**步数 + 墙钟预算**（几乎零成本）。
  Tier 1  SubprocessExecutor 隔离，常驻 worker 子进程 + rlimit/seccomp（best-effort）。
  Tier 2  WasmExecutor       可选加强，**本版不实现**（接口预留，避免重依赖拖慢交付）。

为什么先做 Tier 0：它挡住 99% 的"写死循环 / 大数组"事故，且不改热路径开销。
Tier 1 才是真正的安全边界，用于**不可信/待审**算子或对隔离有硬要求的部署。

诚实边界（§6）：
  - Windows 无 seccomp、无 resource → Tier 1 降级为「进程隔离 + 墙钟超时」，
    `isolation` 字段如实回报实际档位，不假装有 seccomp/rlimit。
  - 子进程有 IPC 开销 → 热路径默认仍 inline。
"""
from __future__ import annotations

import multiprocessing as mp
from dataclasses import dataclass
from typing import Any

from .dsl import SandboxViolation  # 再导出：对外统一从 sandbox 导入
from .pipeline import Operator

__all__ = [
    "SandboxPolicy",
    "SandboxViolation",
    "ExecutorPool",
    "InlineExecutor",
    "SubprocessExecutor",
    "PICKLEABLE_OPERATOR_NOTE",
]

PICKLEABLE_OPERATOR_NOTE = (
    "SubprocessExecutor 须把 Operator 经 multiprocessing 队列 pickle 给子进程；"
    "Operator 及其 field/window 入参须可 pickle（纯数据满足；含不可序列化对象的调用方请用 inline）。"
)


@dataclass(frozen=True)
class SandboxPolicy:
    """沙箱策略。默认值对齐 docs/slo.md §4（单条算子 ≤10ms / ≤32MB）。"""
    wall_s: float = 0.05          # 单算子墙钟上限（热路径须极短）
    max_steps: int = 100_000      # 解释器步数预算（Tier 0）
    mem_mb: int = 64              # Tier 1：子进程地址空间增量上限
    cpu_s: float = 0.1            # Tier 1：CPU 时间 rlimit
    allow_net: bool = False       # Tier 1：默认禁网
    allow_fs: bool = False        # Tier 1：默认禁文件系统写
    kind: str = "inline"          # inline | subprocess（wasm 预留）

    def as_dict(self) -> dict[str, Any]:
        return {
            "wall_s": self.wall_s, "max_steps": self.max_steps,
            "mem_mb": self.mem_mb, "cpu_s": self.cpu_s,
            "allow_net": self.allow_net, "allow_fs": self.allow_fs,
        }


class InlineExecutor:
    """Tier 0：同进程执行，仅加步数/墙钟预算。"""

    isolation = "inline"

    def __init__(self, policy: SandboxPolicy):
        self.policy = policy

    def run(self, op: Operator, event: dict[str, Any],
            window: Any = None) -> list[str]:
        return op.run(event, window,
                      max_steps=self.policy.max_steps, wall_s=self.policy.wall_s)

    def close(self) -> None:
        return None


class SubprocessExecutor:
    """Tier 1：常驻 worker 子进程执行。

    为何常驻而非每算子 fork：fork 开销（尤其 Windows spawn 要重新导入整套包）远大于
    单次求值本身，逐算子 fork 会拖垮吞吐。常驻 worker 起一次、循环收活。

    超时处理：父进程 `get(wall_s+0.5)` 超时 → `_kill()` 杀 worker → 抛 `SandboxViolation`。
    `_kill()` 是**两级**的：先 `terminate()`（SIGTERM，允许 worker 自清理），
    1 秒内不退就升级到 `kill()`（POSIX=SIGKILL / Windows=TerminateProcess）。
    只发 SIGTERM 不够：worker 若卡在 C 层长循环里、信号被屏蔽或处理里又抛异常，
    它就成了**孤儿**——而句柄已于 `_proc = None` 丢弃，`_ensure()` 会再起一个新 worker，
    反复超时就是无界进程/CPU 泄漏（这正是 rlimit 之外必须自己兜住的失败模式）。
    真连 SIGKILL 都无效时（不可 kill 状态）记入 `leaked`，不静默吞掉。
    worker 被杀后**不可复用**（进程已死），故 `_ensure()` 会重启一个——失败关闭：
    宁可多起一个进程，也不留下状态可疑的 worker。
    """

    isolation = "subprocess"

    def __init__(self, policy: SandboxPolicy):
        self.policy = policy
        # spawn：Windows 唯一可用且最安全的启动方式（不继承父地址空间）
        self._ctx = mp.get_context("spawn")
        self._cmd_q: Any = None
        self._res_q: Any = None
        self._proc: Any = None
        self._level = "unknown"   # 子进程回报的实际隔离档位（rlimit/seccomp 组合）
        self._leaked: list[Any] = []   # SIGKILL 都没能终结的 worker（理应永远为空）
        self._start()

    def _start(self) -> None:
        from . import sandbox_exec  # 延迟导入：spawn 子进程会自行导入本模块
        self._cmd_q = self._ctx.Queue()
        self._res_q = self._ctx.Queue()
        self._proc = self._ctx.Process(
            target=sandbox_exec.worker_main,
            args=(self._cmd_q, self._res_q, self.policy.as_dict()),
            daemon=True,
        )
        self._proc.start()
        # 等 worker 报 ready（含实际隔离档位）；给足 spawn 冷启动时间
        try:
            tag, level = self._res_q.get(timeout=30.0)
            if tag == "ready":
                self._level = level
        except Exception:  # noqa: BLE001 — 起不来则标记，run() 时会再处理
            # 带上退出码：否则只剩“启动失败”四个字，无法区分 import 报错、
            # 被信号杀（-9 SIGKILL / -24 SIGXCPU / -11 SIGSEGV）还是资源限。
            # 实测过：Linux 容器里 RLIMIT_CPU 写绝对值就会被 SIGXCPU 杀掉，
            # 而旧代码只报“求值超时”，白花几小时才定位。
            code = None
            try:
                self._proc.join(timeout=2.0)
                code = self._proc.exitcode
            except Exception:  # noqa: BLE001
                code = None
            self._level = f"start-failed(exit={code})" if code is not None \
                else "start-failed(exit=unknown)"

    def _ensure(self) -> None:
        if self._proc is None or not self._proc.is_alive():
            self._start()

    def run(self, op: Operator, event: dict[str, Any],
            window: Any = None) -> list[str]:
        self._ensure()
        if self._level.startswith("start-failed"):
            raise SandboxViolation(f"子进程沙箱 worker 启动失败（{self._level}）")
        self._cmd_q.put((op, event, window,
                         self.policy.max_steps, self.policy.wall_s))
        # 父进程侧墙钟兜底：留一点 IPC 余量（子进程内也有预算检查，此处防挂死）
        try:
            tag, payload = self._res_q.get(timeout=self.policy.wall_s + 0.5)
        except Exception as e:  # noqa: BLE001 — 队列空 = 子进程挂死/被杀
            code = self._proc.exitcode if self._proc is not None else None
            self._kill()   # 注意：_kill 会把 _proc 置 None，退出码必须提前取
            # 带上退出码（-24=SIGXCPU 就表示 CPU rlimit 太紧，不是“代码慢”）
            raise SandboxViolation(
                f"子进程求值超时（已终止 worker，exit={code}）"
            ) from e
        if tag == "ok":
            return payload
        if tag == "violation":
            raise SandboxViolation(payload)
        raise SandboxViolation(f"子进程求值异常: {payload}")

    def _kill(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()          # SIGTERM：给 worker 自清理的机会
            proc.join(timeout=1.0)
            if proc.is_alive():
                # 升级到 SIGKILL。不做这一步，worker 会变成孤儿：句柄已被丢弃、
                # 执行器下次 `_ensure()` 又起一个，反复超时 = 无界进程/CPU 泄漏。
                proc.kill()
                proc.join(timeout=1.0)
        except Exception:  # noqa: BLE001 — 杀进程本身失败不应把调用方拖进异常路径
            pass
        if proc.is_alive():           # 极端情况：连 SIGKILL 都无效（如不可 kill 状态）
            self._leaked.append(proc)
        self._proc = None

    @property
    def leaked(self) -> int:
        """未能终结的 worker 数（正常恒为 0；非 0 意味着宿主上残留了 worker）。"""
        return len(self._leaked)

    def close(self) -> None:
        if self._proc is not None and self._proc.is_alive():
            try:
                self._cmd_q.put(None)          # 优雅退出
                self._proc.join(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
        self._kill()

    def __enter__(self) -> SubprocessExecutor:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


_EXECUTORS = {"inline": InlineExecutor, "subprocess": SubprocessExecutor}


class ExecutorPool:
    """按策略选择/复用执行器。对外唯一入口。

    用法：
        pool = ExecutorPool(SandboxPolicy(kind="subprocess"))
        try:
            hits = pool.run(op, event, window)
        except SandboxViolation:
            ...  # 失败关闭：丢弃该算子该次求值，不中断整条流
        finally:
            pool.close()
    """

    def __init__(self, policy: SandboxPolicy | None = None, kind: str | None = None):
        self.policy = policy or SandboxPolicy()
        chosen = (kind or self.policy.kind or "inline").lower()
        if chosen not in _EXECUTORS:
            raise ValueError(
                f"未知沙箱类型 {chosen!r}（可用: {sorted(_EXECUTORS)}；wasm 为预留，未实现）")
        self._exec: InlineExecutor | SubprocessExecutor
        if chosen == "subprocess":
            self._exec = SubprocessExecutor(self.policy)
        else:
            self._exec = InlineExecutor(self.policy)

    def run(self, op: Operator, event: dict[str, Any],
            window: Any = None) -> list[str]:
        return self._exec.run(op, event, window)

    @property
    def isolation(self) -> str:
        """实际隔离档位（inline / subprocess[rlimit+seccomp] 等），供日志与审计。"""
        if isinstance(self._exec, SubprocessExecutor):
            return f"subprocess[{self._exec._level}]"
        return self._exec.isolation

    def close(self) -> None:
        self._exec.close()

    def __enter__(self) -> ExecutorPool:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def run_isolated(op: Operator, event: dict[str, Any], window: Any = None,
                 policy: SandboxPolicy | None = None,
                 kind: str | None = None) -> list[str]:
    """一次性便捷入口：建池→跑→关池。适合低频/非热路径调用。

    热路径请复用 `ExecutorPool`（起子进程有开销）。任何越权/超时抛
    `SandboxViolation`，由调用方失败关闭。
    """
    pool = ExecutorPool(policy, kind)
    try:
        return pool.run(op, event, window)
    finally:
        pool.close()
