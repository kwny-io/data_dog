"""常驻沙箱 worker（`operator_factory/sandbox_exec.py`）的**进程内**单元测试。

为什么不用真子进程
------------------
真子进程那条链路（spawn → worker_main → 回报）已由 `tests/test_sandbox.py` 的
`ExecutorPool(kind="subprocess")` 用例覆盖；但子进程里的行**记不进本进程的覆盖率**，
而且真起进程无法构造"内核拒绝 setrlimit""pyseccomp 未装""算子抛 MemoryError"
这些分支。因此这里把两类外部依赖换成可编程替身，**在进程内**直接调：

  - `resource` / `seccomp` 用 `sys.modules` 注入假模块 —— 于是 Windows 上也能
    确定性地验证 Unix 资源上限与 seccomp 规则表（否则这些安全代码永远测不到）；
  - `cmd_q` / `res_q` 用假队列 —— 于是"回报协议"与"兜住 BaseException 后仍继续
    常驻"这两条 worker 契约可以被逐条断言，不必依赖父进程与超时。

诚实边界：假模块验证的是**我们下发的策略**（哪个 rlimit 设成多少、哪些系统调用被
拒），不是内核真的执行了隔离——后者只有真子进程 + 真 Linux 才能证明。
"""
from __future__ import annotations

import builtins
import importlib.util
import io
import sys
import types

import pytest

from data_dog.operator_factory import sandbox_exec
from data_dog.operator_factory.dsl import SandboxViolation
from data_dog.operator_factory.pipeline import Operator

# ================================================================ 替身


class _FakeResource:
    """假 `resource` 模块：只记录 setrlimit 调用，可按常量注入失败。"""

    RLIMIT_CPU = 101
    RLIMIT_AS = 102
    RLIMIT_NOFILE = 103
    RLIMIT_NPROC = 104

    def __init__(self, fail: dict | None = None):
        self.calls: list[tuple[int, tuple[int, int]]] = []
        self._fail = dict(fail or {})

    def setrlimit(self, which, limits) -> None:  # noqa: ANN001
        self.calls.append((which, tuple(limits)))
        exc = self._fail.get(which)
        if exc is not None:
            raise exc

    def limits(self, which: int) -> list[tuple[int, int]]:
        return [lim for w, lim in self.calls if w == which]


class _FakeSeccomp:
    """假 `pyseccomp`：记录规则，ALLOW/ERRNO/EPERM 用哨兵对象代替。"""

    ALLOW = "ALLOW"
    EPERM = "EPERM"

    def __init__(self, fail_add: set[str] | None = None, fail_load: bool = False):
        self.fail_add = set(fail_add or ())
        self.fail_load = fail_load
        self.defaction = None
        self.rules: list[tuple[str, str]] = []
        self.loaded = False
        outer = self

        class _Filter:
            def __init__(self, defaction=None):
                outer.defaction = defaction

            def add_rule(self, action, name):  # noqa: ANN001
                if name in outer.fail_add:
                    raise RuntimeError(f"该架构无 {name}")
                outer.rules.append((action, name))

            def load(self):
                if outer.fail_load:
                    raise RuntimeError("seccomp 加载失败")
                outer.loaded = True

        self.SyscallFilter = _Filter

    @staticmethod
    def ERRNO(errno):  # noqa: ANN001
        return f"ERRNO({errno})"


class _FakeQueue:
    """假 multiprocessing 队列：条目预先给定，取空即视为协议违规。"""

    def __init__(self, items=()):
        self.items = list(items)
        self.puts: list = []

    def get(self):
        if not self.items:
            raise AssertionError("worker 多取了一次命令（应在收到 None 后立即退出）")
        return self.items.pop(0)

    def put(self, item) -> None:
        self.puts.append(item)


class _RecordingOperator:
    """记录 run 入参的算子替身（可指定抛出的异常）。"""

    def __init__(self, hits=None, raises: BaseException | None = None):
        self.hits = ["R1"] if hits is None else hits
        self.raises = raises
        self.calls: list[tuple] = []

    def run(self, event, window=None, max_steps=0, wall_s=0.0):  # noqa: ANN001
        self.calls.append((event, window, max_steps, wall_s))
        if self.raises is not None:
            raise self.raises
        return self.hits


def _install_resource(monkeypatch, fake) -> None:
    """注入假 resource 模块（值为 None 时模拟"本平台没有 resource"）。"""
    monkeypatch.setitem(sys.modules, "resource", fake)


def _install_seccomp(monkeypatch, fake) -> None:
    monkeypatch.setitem(sys.modules, "seccomp", fake)


# ================================================================ _current_vms_bytes


def test_current_vms_bytes_parses_statm(monkeypatch):
    """能读到 /proc/self/statm 时：页数 × 页大小（这是 rlimit 增量上限的基准）。"""
    monkeypatch.setattr(builtins, "open", lambda *a, **k: io.StringIO("42 7 3\n"))
    seen: list[str] = []

    def fake_sysconf(name):
        seen.append(name)
        return 4096

    monkeypatch.setattr(sandbox_exec.os, "sysconf", fake_sysconf, raising=False)
    assert sandbox_exec._current_vms_bytes() == 42 * 4096
    assert seen == ["SC_PAGE_SIZE"]
    print("PASS test_current_vms_bytes_parses_statm")


def test_current_vms_bytes_returns_zero_when_unreadable(monkeypatch):
    """读不到/内容畸形一律返回 0（= 不做内存上限），绝不能因此让 worker 起不来。"""
    def _open_raising(exc):
        def _open(*a, **k):
            raise exc
        return _open

    monkeypatch.setattr(builtins, "open", _open_raising(OSError("no /proc")))
    assert sandbox_exec._current_vms_bytes() == 0

    monkeypatch.setattr(builtins, "open", lambda *a, **k: io.StringIO(""))
    assert sandbox_exec._current_vms_bytes() == 0            # IndexError

    monkeypatch.setattr(builtins, "open", lambda *a, **k: io.StringIO("abc\n"))
    assert sandbox_exec._current_vms_bytes() == 0            # ValueError
    print("PASS test_current_vms_bytes_returns_zero_when_unreadable")


# ================================================================ _apply_rlimits


def test_apply_rlimits_without_resource_module(monkeypatch):
    """Windows（无 resource）→ 如实报 no-rlimit，不得假装设了隔离。"""
    _install_resource(monkeypatch, None)
    assert sandbox_exec._apply_rlimits({}) == "no-rlimit"
    print("PASS test_apply_rlimits_without_resource_module")


def test_apply_rlimits_sets_cpu_nofile_nproc(monkeypatch):
    fake = _FakeResource()
    _install_resource(monkeypatch, fake)
    # 把时钟钉死，否则断言只能验“形状”而不能验具体数值
    monkeypatch.setattr(sandbox_exec.time, "process_time", lambda: 10.0)
    assert sandbox_exec._apply_rlimits({"cpu_s": 2.7}) == "rlimit"
    # CPU 限是**增量**：`int(已用 + cpu_s) + 1`，硬限 = 软限 + 1（不可回落）。
    # 绝不能写成绝对值 `int(cpu_s)+1`——worker 光重建导入图就可能烧掉 1s 以上，
    # Linux 容器里会被 SIGXCPU 直接打死（父进程只看到“求值超时”）。
    cpu_soft, cpu_hard = fake.limits(fake.RLIMIT_CPU)[0]
    assert cpu_soft == int(10.0 + 2.7) + 1 == 13, cpu_soft
    assert cpu_hard == cpu_soft + 1 == 14, cpu_hard
    assert cpu_soft > 10.0, "软限必须大于已用 CPU，否则等于立刻自杀"
    assert fake.limits(fake.RLIMIT_NOFILE) == [(16, 16)]
    # NPROC 必须**完全不动**：Linux 的 NPROC 统计同 uid 的全部任务（含线程），
    # 而 CPython 子解释器一启动就要建线程——设成 (0,0) 等于让 worker 一开就死。
    # 且 root 对 NPROC 设限是静默无操作（有 CAP_SYS_RESOURCE），
    # 于是“root 容器绿 + runner 普通用户红”的假绿就是这么漏出去的（2026-10-23 实测）。
    assert fake.limits(fake.RLIMIT_NPROC) == [], \
        "不得对 RLIMIT_NPROC 设限：会把 worker 的线程建死，且 root 下静默无效"
    assert fake.limits(fake.RLIMIT_AS) == []          # 未给 mem_mb → 不动地址空间
    assert [w for w, _ in fake.calls] == [fake.RLIMIT_CPU, fake.RLIMIT_NOFILE]
    print("PASS test_apply_rlimits_sets_cpu_nofile_nproc")


def test_apply_rlimits_cpu_zero_or_missing_is_skipped(monkeypatch):
    for policy in ({}, {"cpu_s": 0}, {"cpu_s": 0.0}, {"cpu_s": None}):
        fake = _FakeResource()
        _install_resource(monkeypatch, fake)
        assert sandbox_exec._apply_rlimits(policy) == "rlimit"
        assert fake.limits(fake.RLIMIT_CPU) == [], policy
    # 小于 1 秒也至少留 1 秒余量（int()+1），不会设成已用量本身把自己立刻打死
    fake = _FakeResource()
    _install_resource(monkeypatch, fake)
    monkeypatch.setattr(sandbox_exec.time, "process_time", lambda: 10.0)
    sandbox_exec._apply_rlimits({"cpu_s": 0.5})
    assert fake.limits(fake.RLIMIT_CPU) == [(11, 12)]
    print("PASS test_apply_rlimits_cpu_zero_or_missing_is_skipped")


def test_apply_rlimits_memory_is_incremental_not_absolute(monkeypatch):
    """RLIMIT_AS = 当前占用 + mem_mb（绝不能设绝对值：Python 基线本就远超它）。"""
    cur = 80 * 1024 * 1024
    monkeypatch.setattr(sandbox_exec, "_current_vms_bytes", lambda: cur)
    fake = _FakeResource()
    _install_resource(monkeypatch, fake)
    assert sandbox_exec._apply_rlimits({"mem_mb": 32}) == "rlimit"
    expected = cur + 32 * 1024 * 1024
    assert fake.limits(fake.RLIMIT_AS) == [(expected, expected)]
    print("PASS test_apply_rlimits_memory_is_incremental_not_absolute")


def test_apply_rlimits_memory_skipped_when_vms_unknown(monkeypatch):
    """读不到当前占用就不设地址空间上限（宁可不设，也不能设成绝对值立刻触顶）。"""
    monkeypatch.setattr(sandbox_exec, "_current_vms_bytes", lambda: 0)
    fake = _FakeResource()
    _install_resource(monkeypatch, fake)
    assert sandbox_exec._apply_rlimits({"mem_mb": 64, "cpu_s": 1}) == "rlimit"
    assert fake.limits(fake.RLIMIT_AS) == []
    print("PASS test_apply_rlimits_memory_skipped_when_vms_unknown")


def test_apply_rlimits_reports_partial_when_kernel_rejects(monkeypatch):
    """内核/权限拒绝某条上限 → 继续设其余的，并如实降档为 rlimit-partial。"""
    monkeypatch.setattr(sandbox_exec, "_current_vms_bytes", lambda: 1024)

    fake = _FakeResource({_FakeResource.RLIMIT_CPU: ValueError("bad")})
    _install_resource(monkeypatch, fake)
    assert sandbox_exec._apply_rlimits({"cpu_s": 3}) == "rlimit-partial"
    assert fake.limits(fake.RLIMIT_NOFILE) == [(16, 16)]     # 后面仍继续设

    fake = _FakeResource({_FakeResource.RLIMIT_AS: OSError("denied")})
    _install_resource(monkeypatch, fake)
    assert sandbox_exec._apply_rlimits({"mem_mb": 8}) == "rlimit-partial"

    fake = _FakeResource({_FakeResource.RLIMIT_NOFILE: ValueError("nope")})
    _install_resource(monkeypatch, fake)
    assert sandbox_exec._apply_rlimits({}) == "rlimit-partial"
    print("PASS test_apply_rlimits_reports_partial_when_kernel_rejects")


def test_apply_rlimits_ignores_unsupported_nproc(monkeypatch):
    """有些平台没有 RLIMIT_NPROC → 属性错误被吞掉，且**不算降档**。"""
    fake = _FakeResource({_FakeResource.RLIMIT_NPROC: AttributeError("no nproc")})
    _install_resource(monkeypatch, fake)
    assert sandbox_exec._apply_rlimits({"cpu_s": 1}) == "rlimit"
    print("PASS test_apply_rlimits_ignores_unsupported_nproc")


# ================================================================ _apply_seccomp


def test_apply_seccomp_skipped_when_policy_allows_both():
    assert sandbox_exec._apply_seccomp(
        {"allow_net": True, "allow_fs": True}) == "seccomp-skip"
    print("PASS test_apply_seccomp_skipped_when_policy_allows_both")


def test_apply_seccomp_without_pyseccomp_is_not_blocking(monkeypatch):
    """未装可选依赖 → 报 no-seccomp 并放行（不阻断 worker 起不来）。"""
    _install_seccomp(monkeypatch, None)
    assert sandbox_exec._apply_seccomp({}) == "no-seccomp"
    print("PASS test_apply_seccomp_without_pyseccomp_is_not_blocking")


def test_apply_seccomp_denies_net_exec_and_fs(monkeypatch):
    fake = _FakeSeccomp()
    _install_seccomp(monkeypatch, fake)
    assert sandbox_exec._apply_seccomp({}) == "seccomp"
    assert fake.defaction == fake.ALLOW        # 白名单式：默认放行、逐条拒绝
    assert fake.loaded
    denied = [name for _, name in fake.rules]
    for expected in ("socket", "connect", "execve", "fork", "open", "openat",
                     "unlink", "rename"):
        assert expected in denied, expected
    assert all(action == f"ERRNO({fake.EPERM})" for action, _ in fake.rules)
    print("PASS test_apply_seccomp_denies_net_exec_and_fs")


def test_apply_seccomp_keeps_fs_open_when_allow_fs(monkeypatch):
    """策略放行文件 → 不拒 open/openat，但网络与 exec 仍要拒（逐项放行，非全开）。"""
    fake = _FakeSeccomp()
    _install_seccomp(monkeypatch, fake)
    assert sandbox_exec._apply_seccomp({"allow_fs": True}) == "seccomp"
    denied = [name for _, name in fake.rules]
    assert "open" not in denied and "openat" not in denied
    assert "socket" in denied and "execve" in denied
    print("PASS test_apply_seccomp_keeps_fs_open_when_allow_fs")


def test_apply_seccomp_skips_syscalls_missing_on_this_kernel(monkeypatch):
    """该内核/架构没有的系统调用要跳过，不能因为一条失败就整体放弃。"""
    fake = _FakeSeccomp(fail_add={"clone3", "renameat"})
    _install_seccomp(monkeypatch, fake)
    assert sandbox_exec._apply_seccomp({}) == "seccomp"
    assert fake.loaded
    denied = [name for _, name in fake.rules]
    assert "clone3" not in denied and "socket" in denied
    print("PASS test_apply_seccomp_skips_syscalls_missing_on_this_kernel")


def test_apply_seccomp_reports_failed_when_load_raises(monkeypatch):
    """加载失败 → 报 seccomp-failed（诚实降级），不让异常掀翻 worker。"""
    _install_seccomp(monkeypatch, _FakeSeccomp(fail_load=True))
    assert sandbox_exec._apply_seccomp({}) == "seccomp-failed"
    print("PASS test_apply_seccomp_reports_failed_when_load_raises")


# ================================================================ _apply_limits


def test_apply_limits_drops_noise_markers_when_something_real(monkeypatch):
    """"no-*" 是"没做到"的噪声：有真档位时不该混进 isolation 描述。"""
    _install_resource(monkeypatch, None)          # → no-rlimit
    _install_seccomp(monkeypatch, _FakeSeccomp())  # → seccomp
    assert sandbox_exec._apply_limits({}) == "seccomp"
    print("PASS test_apply_limits_drops_noise_markers_when_something_real")


def test_apply_limits_keeps_noise_markers_when_nothing_available(monkeypatch):
    """全都做不到时必须**如实**列出 no-rlimit+no-seccomp，不能返回空串假装有隔离。"""
    _install_resource(monkeypatch, None)
    _install_seccomp(monkeypatch, None)
    assert sandbox_exec._apply_limits({}) == "no-rlimit+no-seccomp"
    print("PASS test_apply_limits_keeps_noise_markers_when_nothing_available")


# ================================================================ worker_main 协议


def _run_worker(monkeypatch, items, policy=None):
    """跑一轮 worker_main；把 resource/seccomp 都置为"本机没有"，结果可预期。

    末尾补一个 None：worker 的常驻循环只有收到 None 才退出，替身队列取空即报错，
    故"命令耗尽"必须由 None 显式终止——这也顺带验证了终止协议。
    """
    _install_resource(monkeypatch, None)
    _install_seccomp(monkeypatch, None)
    cmd_q, res_q = _FakeQueue([*items, None]), _FakeQueue()
    sandbox_exec.worker_main(cmd_q, res_q, dict(policy or {}))
    return res_q.puts


def test_worker_main_reports_ready_then_ok(monkeypatch):
    op = _RecordingOperator(hits=["R1", "R2"])
    window = {"w": 1}
    puts = _run_worker(monkeypatch, [(op, {"x": 1}, window, 50, 0.5)])

    assert puts[0][0] == "ready", "必须先回报隔离档位，父进程据此判定能力"
    # 两个可选依赖都缺席 → 如实列全 no-* 标记（不返回空串假装有隔离）
    assert puts[0][1] == "no-rlimit+no-seccomp"
    assert puts[1] == ("ok", ["R1", "R2"])
    # 预算必须真的透传给算子，而不是 worker 自己吞掉
    assert op.calls == [({"x": 1}, window, 50, 0.5)]
    print("PASS test_worker_main_reports_ready_then_ok")


def test_worker_main_exits_on_none_terminator(monkeypatch):
    """None = 优雅退出：只回报 ready 就返回，不再取队列（取空会 AssertionError）。"""
    puts = _run_worker(monkeypatch, [None])
    assert [p[0] for p in puts] == ["ready"]
    print("PASS test_worker_main_exits_on_none_terminator")


def test_worker_main_reports_violation_and_keeps_serving(monkeypatch):
    """越权 → violation 回报；worker 必须继续常驻（下一次派活照跑）。"""
    bad = _RecordingOperator(raises=SandboxViolation("步数超预算"))
    good = _RecordingOperator(hits=["OK"])
    puts = _run_worker(monkeypatch, [(bad, {}, None, 1, 0.0), (good, {}, None, 0, 0.0)])

    assert puts[1] == ("violation", "步数超预算")
    assert puts[2] == ("ok", ["OK"])
    print("PASS test_worker_main_reports_violation_and_keeps_serving")


def test_worker_main_survives_base_exception(monkeypatch):
    """兜住 BaseException：MemoryError 这类不得让 worker 静默死掉，必须回报且继续跑。"""
    bad = _RecordingOperator(raises=MemoryError("memory limit"))
    good = _RecordingOperator(hits=["AFTER"])
    puts = _run_worker(monkeypatch, [(bad, {}, None, 0, 0.0), (good, {}, None, 0, 0.0)])

    assert puts[1] == ("error", "MemoryError: memory limit")
    assert puts[2] == ("ok", ["AFTER"]), "回报后必须继续常驻，而不是退出"
    print("PASS test_worker_main_survives_base_exception")


def test_worker_main_passes_budget_to_real_operator(monkeypatch):
    """接真算子：worker 透传的 max_steps 真的能触发 dsl 的越权保护（不是空跑）。"""
    op = Operator(
        operator_id="sandbox_exec_test", component="unit", input_schema="in",
        output_schema="out", fields=[{"name": "x", "type": "number"}],
        dsl_rules=[{"rule_id": "BIG", "when": " + ".join(["x"] * 200) + " > 0"}],
    )
    puts = _run_worker(monkeypatch, [(op, {"x": 1}, None, 50, 0.0)])
    assert puts[1][0] == "violation"
    assert "步数超预算" in puts[1][1]
    print("PASS test_worker_main_passes_budget_to_real_operator")


def test_worker_main_reports_limits_level_from_policy(monkeypatch):
    """ready 里的档位随策略变：假 resource+seccomp 都生效时报 rlimit+seccomp。"""
    _install_resource(monkeypatch, _FakeResource())
    _install_seccomp(monkeypatch, _FakeSeccomp())
    cmd_q, res_q = _FakeQueue([None]), _FakeQueue()
    sandbox_exec.worker_main(cmd_q, res_q, {"cpu_s": 1})
    assert res_q.puts == [("ready", "rlimit+seccomp")]
    print("PASS test_worker_main_reports_limits_level_from_policy")


def test_module_is_spawn_importable():
    """spawn 的硬要求：worker_main 必须是可导入的模块级函数（不能是闭包/局部）。"""
    spec = importlib.util.find_spec("data_dog.operator_factory.sandbox_exec")
    assert spec is not None and spec.origin is not None
    assert callable(sandbox_exec.worker_main)
    assert isinstance(sandbox_exec.worker_main, types.FunctionType)
    assert sandbox_exec.worker_main.__module__ == "data_dog.operator_factory.sandbox_exec"
    print("PASS test_module_is_spawn_importable")


def test_fake_queue_helpers_behave(monkeypatch):
    """替身自检：假队列取空必须炸（否则上面的"优雅退出"用例会假绿）。"""
    q = _FakeQueue([1])
    assert q.get() == 1
    with pytest.raises(AssertionError, match="多取了一次命令"):
        q.get()
    print("PASS test_fake_queue_helpers_behave")
