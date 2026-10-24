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
import time
from typing import Any, cast


def _current_vms_bytes() -> int:
    """当前虚拟地址空间大小（字节）。非 Linux / 读不到时返回 0。"""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[0])
        # cast 到 Any 再取 sysconf：它仅在 Unix 存在（能读到 statm 即是 Unix，运行时安全），
        # 但 Windows 存根无此属性会被 mypy 报 attr-defined，而平台条件 ignore 又会在
        # Linux CI 上变 unused-ignore——cast 两头都干净。
        return pages * cast(Any, os).sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


def _apply_rlimits(policy: dict[str, Any]) -> str:
    """在子进程内设 Unix 资源上限。返回档位标记字符串。

    **关键取舍**：RLIMIT_AS 设为「当前占用 + mem_mb」，而非绝对值 mem_mb。
    因为 spawn（及 Unix fork）出的 Python 子进程继承/重建的地址空间本就远超
    64MB，若设绝对值会**立刻触顶**、连正常求值都跑不了。按"增量余量"设上限，
    才能在保留 Python 运行时基线的前提下真正约束算子自身的分配。
    """
    # 动态导入并显式标注 Any：`import resource` 会让 mypy 用平台存根解析——
    # Windows 存根无 setrlimit/RLIMIT_*，报 attr-defined；而平台条件 ignore 又会在
    # Linux CI 上变 unused-ignore。importlib + Any 在两种平台都干净。
    import importlib
    try:
        resource: Any = importlib.import_module("resource")
    except ImportError:
        return "no-rlimit"  # Windows：无 resource 模块

    level = "rlimit"
    cpu = float(policy.get("cpu_s", 0.0) or 0.0)
    if cpu > 0:
        # 软/硬都设：软限到点内核发 SIGXCPU（默认终止），硬限是不可回落的上限
        #
        # **按「已用 + 余量」设，不是绝对值**（与下面 RLIMIT_AS 同一哲学）：
        # spawn 子进程启动时先重建整个包的导入图（`data_dog/__init__` 拉起
        # agent/providers/…），这段 CPU 在设限之前就已经烧掉了。若把软限写成
        # 绝对值 `int(cpu)+1`，那么 cpu_s=0.1 时软限就是 1s，而容器/慢机上光导入
        # 就 > 1s——内核下一个计时 tick 直接 SIGXCPU 杀掉 worker，父进程什么也收不到，
        # 报“子进程求值超时”。实测：Windows（无 resource）全绿，Linux 容器 3 项红。
        try:
            used = time.process_time()
            soft = int(used + cpu) + 1
            resource.setrlimit(resource.RLIMIT_CPU, (soft, soft + 1))
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

    # **禁止用 RLIMIT_NPROC 来「禁子进程」**（曾在此设 (0, 0)，已移除）：
    # Linux 的 NPROC 配额统计的是**同一 real UID 下的全部 task，包括线程**。
    # 而本 worker 立刻就要用 `mp.Queue`——它首次 put 时 lazy 建 feeder 线程，
    # 于是 `worker_main` 里「先 `_apply_limits()`、后 `res_q.put(("ready", ...))`」
    # 的顺序必然撞墙：`RuntimeError: can't start new thread`。
    #
    # **为什么本地一直是绿的、CI 一上就红**：root 带 `CAP_SYS_RESOURCE`，
    # 内核对 RLIMIT_NPROC 的检查被绕过；GitHub runner 以普通用户 `runner` 跑，
    # 限制真实生效。实测证据（同镜像同代码，仅换用户）：
    #   `docker run --rm --user 65534 … pytest tests/test_sandbox.py`
    #   → 6 failed, 10 passed（全部 `can't start new thread`）
    #   `docker run --rm …`（root）→ 16 passed
    # 即「假绿」的根子不是平台差异本身，而是**用错了手段**：
    # 禁 fork 的正确层次是 `_apply_seccomp()`（已 deny fork/vfork/clone/clone3），
    # 而 worker 自身不调用 fork 是代码纪律。NPROC 做不到「只禁进程、放行线程」。
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
    except (ValueError, OSError):
        level = "rlimit-partial"

    return level


def _apply_seccomp(policy: dict[str, Any]) -> str:
    """可选 seccomp：禁 socket/connect/open/execve（仅 Linux + pyseccomp）。

    best-effort：未装 `pyseccomp` 或非 Linux → 返回 `no-seccomp`，**不阻断**。
    """
    if policy.get("allow_net") and policy.get("allow_fs"):
        return "seccomp-skip"  # 策略显式放行，无需收紧
    try:
        import seccomp  # 可选依赖 pyseccomp（ignore_missing_imports 已覆盖缺存根）
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


def _trace(msg: str) -> None:
    """bootstrap 追踪（仅在 `DD_SANDBOX_TRACE` 设了文件路径时写）。

    为什么需要：worker 起不来时父进程只能等 30s 拿到空队列，无法区分
    “卡在 import”“卡在设 rlimit”“卡在建 Queue 线程”“已经 put 但父进程没收到”。
    真踩过：全量套件里 6 条 spawn 用例全部 `start-failed(exit=unknown)`（进程还活着），
    单文件跑全绿——没有分阶段痕迹就只能猜。
    """
    path = os.environ.get("DD_SANDBOX_TRACE")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {time.time():.3f} {msg}\n")
    except Exception:  # noqa: BLE001 — 追踪绝不能影响沙箱本身
        pass


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

    _trace("worker-start")
    level = _apply_limits(policy)
    _trace(f"limits-applied:{level}")
    res_q.put(("ready", level))
    _trace("ready-sent")

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
