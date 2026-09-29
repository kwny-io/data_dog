"""Tier 1 子进程沙箱的 worker 入口（可被 multiprocessing spawn 导入）。

为什么单独一个模块
------------------
Windows 的 `spawn` 启动方式要求 worker 的目标函数是**模块级可导入**的（不能是
局部函数/闭包）。把入口隔离在此模块，父进程 `sandbox.SubprocessExecutor` 只需
`Process(target=sandbox_exec.worker_main, ...)` 即可跨平台启动。

worker 的职责
-------------
1. 启动时**自设资源上限**（Unix：rlimit；Linux：可选 seccomp）——这一步在**子进程内**
   做，故即使父进程无权限，也由子进程在自身地址空间生效。
2. 常驻循环：从命令队列取 `(operator, event, window, max_steps, wall_s)`，执行
   `operator.run(...)`，把结果/违规回报父进程。
3. **兜住一切异常**（`BaseException`）：子进程内的段错误级问题或 MemoryError
   必须回报而非静默死掉，否则父进程只能靠超时判定——回报更精确、更可诊断。

诚实边界（见 docs/p4/01_sandbox.md §6）
--------------------------------------
- **Windows 无 `resource` 模块** → rlimit 全部跳过，隔离降级为「进程隔离 + 墙钟超时」，
  `isolation_level` 报 `no-rlimit`。网络/文件隔离为 best-effort。
- **seccomp 是可选依赖**（`pyseccomp`）；未安装则跳过并报 `no-seccomp`，不阻断。
"""
from __future__ import annotations

import os
from typing import Any


def _current_vms_bytes() -> int:
    """当前虚拟地址空间大小（字节）。非 Linux / 读不到时返回 0。"""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[0])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


def _apply_rlimits(policy: dict[str, Any]) -> str:
    """在子进程内设 Unix 资源上限。返回档位标记字符串。

    **关键取舍**：RLIMIT_AS 设为「当前占用 + mem_mb」，而非绝对值 mem_mb。
    因为 spawn（及 Unix fork）出的 Python 子进程继承/重建的地址空间本就远超
    64MB，若设绝对值会**立刻触顶**、连正常求值都跑不了。按"增量余量"设上限，
    才能在保留 Python 运行时基线的前提下真正约束算子自身的分配。
    """
    try:
        import resource
    except ImportError:
        return "no-rlimit"  # Windows：无 resource 模块

    level = "rlimit"
    cpu = float(policy.get("cpu_s", 0.0) or 0.0)
    if cpu > 0:
        # 软/硬都设：软限到点内核发 SIGXCPU（默认终止），硬限是不可回落的上限
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (int(cpu) + 1, int(cpu) + 2))
        except (ValueError, OSError):
            level = "rlimit-partial"

    mem_mb = int(policy.get("mem_mb", 0) or 0)
    if mem_mb > 0:
        cur = _current_vms_bytes()
        if cur > 0:
            limit = cur + mem_mb * 1024 * 1024
            try:
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            except (ValueError, OSError):
                level = "rlimit-partial"

    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
    except (ValueError, OSError):
        level = "rlimit-partial"

    # 禁子进程（daemon 进程本就不能再 fork；这条是纵深防御）
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    except (ValueError, OSError, AttributeError):
        pass

    return level


def _apply_seccomp(policy: dict[str, Any]) -> str:
    """可选 seccomp：禁 socket/connect/open/execve（仅 Linux + pyseccomp）。

    best-effort：未装 `pyseccomp` 或非 Linux → 返回 `no-seccomp`，**不阻断**。
    """
    if policy.get("allow_net") and policy.get("allow_fs"):
        return "seccomp-skip"  # 策略显式放行，无需收紧
    try:
        import seccomp  # type: ignore[import-not-found]  # 可选依赖 pyseccomp
    except ImportError:
        return "no-seccomp"

    try:
        f = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
        denied = ["socket", "socketpair", "connect", "bind", "sendto", "recvfrom",
                  "execve", "execveat", "fork", "vfork", "clone", "clone3"]
        if not policy.get("allow_fs"):
            denied += ["open", "openat", "creat", "unlink", "unlinkat", "rename",
                       "renameat", "mkdir", "mkdirat", "truncate", "ftruncate"]
        for name in denied:
            try:
                f.add_rule(seccomp.ERRNO(seccomp.EPERM), name)
            except (RuntimeError, ValueError):
                pass  # 该内核/架构无此系统调用，跳过
        f.load()
        return "seccomp"
    except Exception:  # noqa: BLE001 — 加载失败不应让 worker 起不来
        return "seccomp-failed"


def _apply_limits(policy: dict[str, Any]) -> str:
    parts = [_apply_rlimits(policy), _apply_seccomp(policy)]
    # 去掉 no-* 噪声标记，保留真正的档位描述（便于日志一眼看出实际能力）
    meaningful = [p for p in parts if not p.startswith("no-")]
    return "+".join(meaningful) if meaningful else "+".join(parts)


def worker_main(cmd_q, res_q, policy: dict[str, Any]) -> None:  # noqa: ANN001
    """常驻 worker 主循环。父进程通过 cmd_q 派活、从 res_q 收结果。

    协议：
      (None,)                              → 优雅退出
      (operator, event, window, ms, wall)  → 执行，回报
    回报：("ok", hits) | ("violation", msg) | ("error", msg)
    """
    from .dsl import SandboxViolation

    level = _apply_limits(policy)
    res_q.put(("ready", level))

    while True:
        item = cmd_q.get()
        if item is None:
            return
        operator, event, window, max_steps, wall_s = item
        try:
            hits = operator.run(event, window, max_steps=max_steps, wall_s=wall_s)
            res_q.put(("ok", hits))
        except SandboxViolation as e:
            res_q.put(("violation", str(e)))
        except BaseException as e:  # noqa: BLE001 — 子进程须兜住一切并回报
            res_q.put(("error", f"{type(e).__name__}: {e}"))
