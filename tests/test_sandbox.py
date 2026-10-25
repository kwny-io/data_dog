"""P4-1 算子沙箱运行时测试（对齐 docs/p4/01_sandbox.md §5 验收）。

覆盖：
  1. step 预算：超长表达式 → SandboxViolation，宿主快速返回
  2. 墙钟超时：超预算 → 按期终止
  3. 零回归：默认 inline + 大预算，语义不变
  4. 隔离性：子进程崩溃不拖垮父进程
  5. Tier 1 子进程：正常结果与 inline 一致；越权/超时失败关闭
  6. 诚实档位：isolation 如实回报（Windows 无 rlimit/seccomp 不假装有）

跨平台说明：Tier 1 用 multiprocessing spawn，Windows 亦支持；rlimit/seccomp
断言只在对应平台跑（否则测试本身会变成"假装有隔离"）。
"""
from __future__ import annotations

import multiprocessing as mp
import sys
import time
from queue import Full

import pytest

from data_dog.operator_factory.dsl import (
    DSLError,
    SandboxViolation,
    WindowContext,
    compile_rule,
    evaluate,
)
from data_dog.operator_factory.pipeline import Operator
from data_dog.operator_factory.sandbox import (
    ExecutorPool,
    InlineExecutor,
    SandboxPolicy,
    SubprocessExecutor,
    run_isolated,
)


def _op_with_dsl(rule_id: str, when: str, fields: list[str]) -> Operator:
    return Operator(
        operator_id="sandbox_test", component="unit", input_schema="in",
        output_schema="out",
        fields=[{"name": f, "type": "number"} for f in fields],
        dsl_rules=[{"rule_id": rule_id, "when": when}],
    )


# ---------------------------------------------------------------- Tier 0：step 预算

def test_step_budget_violation_fast():
    """超长表达式（左深链 x+x+...）超步数 → SandboxViolation，且宿主快速返回。

    注：项数须 < Python 递归上限——递归下降的编译期 `check()` 本身会按表达式深度
    递归，超大表达式在**编译期**就抛 RecursionError（另一条独立的资源边界，属离线
    算子编写期，非热路径）。这里用 200 项确保能编译，从而真正打中**求值期**预算。
    """
    n = 200
    when = " + ".join(["x"] * n) + " > 0"
    r = compile_rule({"rule_id": "BIG", "when": when}, declared_fields=["x"])
    t0 = time.monotonic()
    with pytest.raises(SandboxViolation, match="步数超预算"):
        evaluate(r, {"x": 1}, max_steps=50)
    assert time.monotonic() - t0 < 0.2, "超步应在 200ms 内快速失败，不拖慢宿主"


def test_step_budget_disabled_by_default():
    """默认 max_steps=0 → 不限步，语义与既有行为一致（不误伤正常表达式）。"""
    r = compile_rule({"rule_id": "OK", "when": "x > 0"}, declared_fields=["x"])
    assert evaluate(r, {"x": 1}) is True


# ---------------------------------------------------------------- Tier 0：墙钟超时

def test_wall_clock_violation_on_large_window(monkeypatch):
    """超大 window 输入在极小墙钟预算下 → SandboxViolation（防单个大聚合拖死）。

    为什么把时钟打桩而不是用 `wall_s=1e-9` 靠真实时钟"自然越界"：Windows 上
    `time.monotonic()` 的粒度会让"构造预算"与"第一次检查"落在同一个 tick 里
    （差值 0.0）→ 该用例在**负载高**时反而变成 DID NOT RAISE（实测全量跑挂、
    单独跑必过）。改成确定性时钟后，与机器快慢和负载无关。
    """
    from data_dog.operator_factory import dsl

    calls = {"n": 0}

    def fake_clock() -> float:
        calls["n"] += 1
        return 0.0 if calls["n"] <= 2 else 10.0      # 首次检查在预算内，之后必然越界
    monkeypatch.setattr(dsl.time, "monotonic", fake_clock)

    r = compile_rule({"rule_id": "W", "when": "window.mean(x) > 0"},
                     declared_fields=["x"])
    big = WindowContext(series={"x": [1.0] * 2_000_000})
    with pytest.raises(SandboxViolation):
        # 极小墙钟预算 + max_steps 放开：逼墙钟路径生效
        evaluate(r, {}, big, max_steps=0, wall_s=1e-6)


# ---------------------------------------------------------------- SandboxViolation 不被吞

def test_violation_not_masked_as_miss():
    """evaluate() 的容错不得把沙箱越权伪装成"不命中"（安全事件须向上抛）。"""
    r = compile_rule({"rule_id": "V", "when": "x > 0"}, declared_fields=["x"])
    with pytest.raises(SandboxViolation):
        evaluate(r, {"x": 1}, max_steps=1)  # 极低预算必越界


def test_dsl_error_still_swallowed():
    """对照：普通求值异常仍按"不命中"吞掉（架构 §10.3），只有沙箱越权例外。"""
    r = compile_rule({"rule_id": "Z", "when": "x / 0 > 0"}, declared_fields=["x"])
    assert evaluate(r, {"x": 1}) is False  # 除零 → 不命中，不抛


# ---------------------------------------------------------------- 零回归：默认语义不变

def test_default_inline_semantics_unchanged():
    op = _op_with_dsl("R1", "temperature >= 75", ["temperature"])
    assert op.run({"temperature": 80}) == ["R1"]
    assert op.run({"temperature": 70}) == []


def test_inline_executor_matches_operator_run():
    op = _op_with_dsl("R1", "temperature >= 75", ["temperature"])
    ex = InlineExecutor(SandboxPolicy())
    assert ex.run(op, {"temperature": 80}) == op.run({"temperature": 80})


# ---------------------------------------------------------------- Tier 1：子进程执行

def test_subprocess_matches_inline_result():
    op = _op_with_dsl("R1", "temperature >= 75", ["temperature"])
    with ExecutorPool(SandboxPolicy(kind="subprocess")) as pool:
        assert pool.run(op, {"temperature": 80}) == ["R1"]
        assert pool.run(op, {"temperature": 70}) == []


def test_subprocess_violation_fail_closed():
    """子进程内超步 → 回报 violation，父进程存活并可继续用（worker 重置）。"""
    op = _op_with_dsl("BIG", " + ".join(["x"] * 200) + " > 0", ["x"])
    policy = SandboxPolicy(kind="subprocess", max_steps=50)
    with ExecutorPool(policy) as pool:
        with pytest.raises(SandboxViolation):
            pool.run(op, {"x": 1})
        # 父进程仍可用：换一个正常算子继续跑
        ok = _op_with_dsl("R2", "x > 0", ["x"])
        assert pool.run(ok, {"x": 1}) == ["R2"]


def test_subprocess_crash_does_not_kill_host():
    """隔离性：worker 内触发一次异常 → 父进程捕获，宿主（本测试进程）不受影响。"""
    op = _op_with_dsl("R", "x > 0", ["x"])
    policy = SandboxPolicy(kind="subprocess")
    pool = SubprocessExecutor(policy)
    try:
        pool._kill()  # 模拟 worker 崩溃/被杀
        # 下一次 run 应自动重启 worker 并成功（失败关闭 + 自愈）
        assert pool.run(op, {"x": 1}) == ["R"]
    finally:
        pool.close()


class _StubbornProc:
    """模拟“忽略 SIGTERM 的 worker”：terminate 后仍存活，只有 kill 才死。

    这不是假想：worker 卡在 C 层长循环里、或信号处理里又抛异常时，
    SIGTERM 可能迟迟不生效；父进程若不升级到 SIGKILL，就成了孤儿进程泄漏。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.killed = False

    def terminate(self) -> None:
        self.calls.append("terminate")   # SIGTERM 被忽略

    def kill(self) -> None:
        self.calls.append("kill")
        self.killed = True

    def join(self, timeout: float | None = None) -> None:  # noqa: ARG002
        return None

    def is_alive(self) -> bool:
        return not self.killed

    def exitcode(self) -> None:
        return None


def test_kill_escalates_to_sigkill_when_sigterm_ignored():
    """`_kill()` 必须在 SIGTERM 无效时升级到 SIGKILL，否则 worker 变孤儿。"""
    policy = SandboxPolicy(kind="subprocess")
    pool = SubprocessExecutor(policy)
    try:
        fake = _StubbornProc()
        pool._proc = fake          # 顶掉真 worker，只测升级逻辑
        pool._kill()
        assert fake.calls == ["terminate", "kill"], "缺少 SIGKILL 升级 → 孤儿进程泄漏"
        assert fake.killed is True
        assert pool._proc is None
        assert pool.leaked == 0
    finally:
        pool.close()


def test_kill_records_leak_when_even_sigkill_fails():
    """连 SIGKILL 都无效时要**记下来**，不能静默当成已清理。"""
    policy = SandboxPolicy(kind="subprocess")
    pool = SubprocessExecutor(policy)
    try:
        class _Unkillable(_StubbornProc):
            def kill(self) -> None:      # noqa: D401 — SIGKILL 也失败
                self.calls.append("kill")

        fake = _Unkillable()
        pool._proc = fake
        pool._kill()
        assert pool.leaked == 1, "未终结的 worker 必须计入 leaked，供上层观测"
    finally:
        pool.close()


def test_run_isolated_helper():
    op = _op_with_dsl("R", "x > 0", ["x"])
    assert run_isolated(op, {"x": 1}, kind="subprocess") == ["R"]


# ---------------------------------------------------------------- 诚实档位

def _cpu_rlimit_child(q) -> None:  # noqa: ANN001
    """子进程：先烧掉 >1s CPU（模拟 spawn 后重建导入图），再造限，然后存活上报。"""
    from data_dog.operator_factory.sandbox_exec import _apply_rlimits

    while time.process_time() < 1.3:
        sum(i * i for i in range(50_000))
    _apply_rlimits({"cpu_s": 0.1, "mem_mb": 0})  # 只看 CPU 这一条
    time.sleep(0.4)  # 等内核计时 tick 结算（SIGXCPU 在下个 tick 发出）
    q.put("alive")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 无 resource 模块，无 rlimit 可测")
def test_cpu_rlimit_is_incremental_not_absolute():
    """回归守卫：`RLIMIT_CPU` 必须按「已用 + 余量」设，不能是绝对值。

    为什么需要这条：默认 `cpu_s=0.1`，若写成绝对值 `int(cpu_s)+1` 就是 1s；
    而 spawn 子进程启动时先重建整个包的导入图（实测容器内 3.0s CPU），
    一设限就被内核杀掉（exitcode -9/-24）——父进程什么也收不到，报“子进程求值超时”。
    Windows 无 `resource` 模块，整条路径被跳过，所以在 Windows 上永远绿：
    这个缺陷只在 Linux 暴露，正是“CI 从没真绿”那一类。
    """
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_cpu_rlimit_child, args=(q,), daemon=True)
    p.start()
    try:
        assert q.get(timeout=30.0) == "alive"
    finally:
        p.join(5.0)
    assert p.exitcode == 0, f"worker 被 rlimit 杀掉：exitcode={p.exitcode}（-24=SIGXCPU, -9=KILL）"


def test_isolation_level_reported_honestly():
    with ExecutorPool(SandboxPolicy(kind="subprocess")) as pool:
        level = pool.isolation
    assert level.startswith("subprocess["), level
    if sys.platform == "win32":
        # Windows 无 resource/seccomp → 必须如实报 no-rlimit，不得假装有
        assert "no-rlimit" in level or "seccomp-skip" in level, level
    else:
        assert "rlimit" in level, level


def test_unknown_kind_rejected():
    with pytest.raises(ValueError, match="未知沙箱类型"):
        ExecutorPool(kind="wasm")  # wasm 预留未实现，须明确拒绝而非静默降级


def test_settings_sandbox_policy(monkeypatch):
    from data_dog import settings
    monkeypatch.setenv("DD_SANDBOX_KIND", "subprocess")
    monkeypatch.setenv("DD_SANDBOX_WALL_S", "0.2")
    monkeypatch.setenv("DD_SANDBOX_MEM_MB", "32")
    p = settings.sandbox_policy()
    assert p.kind == "subprocess" and p.wall_s == 0.2 and p.mem_mb == 32
    assert settings.sandbox_kind() == "subprocess"


def test_compile_rule_still_raises_dsl_error():
    """沙箱改动不得影响编译期错误语义（白名单外函数仍拒绝）。"""
    with pytest.raises(DSLError, match="未知函数"):
        compile_rule({"rule_id": "X", "when": "evil(x)"}, declared_fields=["x"])


class _FullQueue:
    """模拟“投递进已满且无人消费的管道”：put 抛 queue.Full。

    为什么这不是假想：worker 已死（例如被 SIGKILL 或卡在 C 层）时，管道缓冲区
    一旦写满，multiprocessing 的 put() 没有任何超时，父进程就会**永久阻塞**——
    现象是进程不动、不打栈、不报错（实测形态：写出 0 字节日志后死等）。
    """

    def put(self, item: object, timeout: float | None = None) -> None:  # noqa: ARG002
        raise Full


def test_subprocess_put_timeout_fails_named_instead_of_hanging():
    """worker 不消费时，投递必须有界并报具名错误，而不是把宿主挂死。"""
    op = _op_with_dsl("R", "x > 0", ["x"])
    pool = SubprocessExecutor(SandboxPolicy(kind="subprocess"))
    try:
        pool._ensure()                 # 起 worker，拿到真实 policy/wall_s
        pool._cmd_q = _FullQueue()     # 之后所有投递都满
        started = time.monotonic()
        with pytest.raises(SandboxViolation) as exc:
            pool.run(op, {"x": 1})
        elapsed = time.monotonic() - started
        msg = str(exc.value)
        assert "投递超时" in msg            # 说清是投递侧，不是“求值超时”
        assert "exit=" in msg               # 带退出码，便于区分死因
        # 有界：不会等满整个 wall_s 之外还拖下去（wall_s 默认 3s + 1s 余量）
        assert elapsed < 10.0
        assert pool._proc is None           # 已清理 worker，不留孤儿
    finally:
        pool.close()
