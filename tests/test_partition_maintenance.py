"""分区维护钩子（service/partition_maintenance.py）：默认不动、开了才补、错了就抛。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from data_dog.service import partition_maintenance as pm


@contextmanager
def _fake_scope(*_a, **_kw):
    yield object()


def test_disabled_by_default_never_touches_the_database(monkeypatch):
    """默认关：连 session 都不该开（结构变更不该悄悄发生在应用进程里）。"""
    def _boom(*_a, **_kw):
        raise AssertionError("默认关时不该打开数据库会话")

    monkeypatch.delenv("DD_PARTITION_AUTO_ENSURE", raising=False)
    monkeypatch.setattr(pm, "session_scope", _boom)
    assert pm.ensure_if_enabled() == []


class _RecordingLog:
    """记录桩：**不用 caplog**。

    为什么：全量跑时别的用例会 setup_logging（改全局 handler/propagate），caplog 就可能啥也抓不到——
    于是"单独跑绿、全量跑红"。直接替掉模块的 logger 就与全局日志配置无关了。
    """

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def warning(self, msg, *args, **_kw):
        self.warnings.append(msg % args if args else msg)

    def info(self, msg, *args, **_kw):
        self.infos.append(msg % args if args else msg)


def test_enabled_but_not_partitioned_logs_and_returns(monkeypatch):
    """显式开了、但表不是分区表：记 warning 后继续（服务本身可用）。"""
    rec = _RecordingLog()
    monkeypatch.setenv("DD_PARTITION_AUTO_ENSURE", "1")
    monkeypatch.setattr(pm, "session_scope", _fake_scope)
    monkeypatch.setattr(pm, "_log", rec)
    monkeypatch.setattr(pm.partitioning, "is_partitioned",
                        lambda _s, _t=pm.partitioning.PARTITION_TABLE: False)
    assert pm.ensure_if_enabled() == []
    assert any("不是分区表" in m for m in rec.warnings)


def test_enabled_ensures_configured_months_and_reports_horizon(monkeypatch):
    """开了就按 DD_PARTITION_ENSURE_MONTHS 补齐，并**顺手报一次**有效期。"""
    seen: dict = {}

    def _ensure(_s, months=3, **_kw):
        seen["months"] = months
        return ["telemetry_events_202610", "telemetry_events_202611"]

    monkeypatch.setenv("DD_PARTITION_AUTO_ENSURE", "1")
    monkeypatch.setenv("DD_PARTITION_ENSURE_MONTHS", "5")
    monkeypatch.setattr(pm, "session_scope", _fake_scope)
    monkeypatch.setattr(pm.partitioning, "is_partitioned", lambda _s, _t=pm.partitioning.PARTITION_TABLE: True)
    monkeypatch.setattr(pm.partitioning, "ensure_partitions", _ensure)
    monkeypatch.setattr(pm, "report_horizon",
                        lambda _s, _t=pm.partitioning.PARTITION_TABLE: seen.setdefault("reported", True) or 1.0)

    assert pm.ensure_if_enabled() == ["telemetry_events_202610", "telemetry_events_202611"]
    assert seen["months"] == 5
    assert seen.get("reported") is True


def test_report_horizon_sets_the_timestamp_gauge(monkeypatch):
    """上报的是**时间戳**（epoch 秒），不是"还剩几天"——Gauge 会陈旧，见 core/metrics.py。"""
    from prometheus_client import generate_latest

    from data_dog.core import metrics

    monkeypatch.setattr(
        pm.partitioning, "list_partitions",
        lambda _s, _t=pm.partitioning.PARTITION_TABLE: ["telemetry_events_202610"])
    end = pm.report_horizon(object())
    assert end == datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()

    line = next(ln for ln in generate_latest(metrics.registry).decode().splitlines()
                if ln.startswith("data_dog_partition_end_timestamp_seconds{")
                and 'kind="future"' in ln)
    assert float(line.rsplit(" ", 1)[1]) == end


def test_report_horizon_stays_silent_when_unknown(monkeypatch):
    """不知道就**不上报**：样本缺失由 `absent(...)` 那条告警负责说，不要往 Gauge 写 0。"""
    monkeypatch.setattr(
        pm.partitioning, "list_partitions",
        lambda _s, _t=pm.partitioning.PARTITION_TABLE: [])
    assert pm.report_horizon(object()) is None


def test_real_errors_propagate_so_the_caller_can_log_them(monkeypatch):
    """真错误（DDL 被拒 / 连不上）**照常抛**：静默跳过和真错误不是一回事。"""
    def _boom(*_a, **_kw):
        raise RuntimeError("permission denied for schema public")

    monkeypatch.setenv("DD_PARTITION_AUTO_ENSURE", "1")
    monkeypatch.setattr(pm, "session_scope", _fake_scope)
    monkeypatch.setattr(pm.partitioning, "is_partitioned", lambda _s, _t=pm.partitioning.PARTITION_TABLE: True)
    monkeypatch.setattr(pm.partitioning, "ensure_partitions", _boom)
    with pytest.raises(RuntimeError):
        pm.ensure_if_enabled()


def test_app_startup_survives_a_failing_partition_hook(monkeypatch):
    """启动钩子失败**不阻止启动**（/metrics + 告警兜底），但不能把异常吞成"成功"。"""
    # 注意：`from data_dog.service import app` 拿到的是 **FastAPI 实例**（包 __init__ 里
    # 已经 `from .app import app`，把子模块名遮蔽了）——要模块本身得走 importlib。
    import importlib

    app_mod = importlib.import_module("data_dog.service.app")
    calls: list[str] = []

    def _boom():
        calls.append("called")
        raise RuntimeError("no partition of relation")

    # 把 lifespan 的**其余**副作用全换掉：真跑会 setup_logging（改全局日志配置）并启停
    # 进程内线程池——在测试里跑会污染别的用例（实测全量跑时让日志类断言集体失灵）。
    for name in ("setup_logging", "_warn_if_shared_state_unavailable",
                 "_start_stream_if_enabled", "_start_job_runner_if_needed",
                 "_start_checkpoint_if_enabled", "_start_escalation_if_enabled",
                 "start_writer_if_enabled", "stop_writer",
                 "_stop_escalation", "_stop_job_runner", "_stop_checkpoint"):
        monkeypatch.setattr(app_mod, name, lambda *a, **k: None)
    monkeypatch.setattr(app_mod.migrations, "auto_migrate_enabled", lambda: False)
    monkeypatch.setattr(app_mod, "_ensure_partitions_if_enabled", _boom)
    # _lifespan 是 async generator：只驱动到 yield 之前（启动段）。
    import asyncio

    async def _run_startup() -> None:
        gen = app_mod._lifespan(app_mod.app)
        await gen.__anext__()
        await gen.aclose()

    asyncio.run(_run_startup())
    assert calls == ["called"]

# --------------------------------------------- 分区 DDL 互斥（多副本 / A9）

@pytest.fixture()
def clean_state():
    """每个用例一把干净的共享状态：租约存在进程内单例里，用例之间不能互相串。"""
    from data_dog.state import reset_state_store

    reset_state_store()
    yield
    reset_state_store()


def _hold_ddl_lease(*, owner: str = "other-replica", ttl_s: float = 300.0):
    """模拟「另一个副本正在做结构变更」：以别的 owner 抢住同一把租约。"""
    from data_dog.partition_ddl import PARTITION_DDL_LEASE_KEY
    from data_dog.state import Lease, get_state_store

    holder = Lease(get_state_store(), PARTITION_DDL_LEASE_KEY, owner, ttl_s=ttl_s)
    assert holder.acquire(), "测试自己都没抢到租约"
    return holder


def test_guard_yields_no_blocker_when_nobody_holds_it(clean_state):
    from data_dog.partition_ddl import ddl_guard

    with ddl_guard() as blocker:
        assert blocker is None


def test_guard_reports_the_other_holder(clean_state):
    """别人持锁 → 产出**持有者标识**（调用方据此「跳过 + 说清被谁挡的」）。"""
    from data_dog.partition_ddl import ddl_guard

    holder = _hold_ddl_lease()
    try:
        with ddl_guard() as blocker:
            assert blocker == "other-replica"
    finally:
        holder.release()


def test_guard_releases_the_lease_on_exit(clean_state):
    """干完就还锁：单副本连续两轮都能跑，别的副本不必等 TTL 过期。"""
    from data_dog.partition_ddl import ddl_guard, ddl_lease_holder

    with ddl_guard() as blocker:
        assert blocker is None
        assert ddl_lease_holder() != "", "拿到租约却没写进共享状态"
    assert ddl_lease_holder() == "", "跑完没释放——别的副本会被永久挡住"


def test_guard_off_switch_ignores_a_held_lease(clean_state, monkeypatch):
    """显式关掉互斥：单机直跑不碰共享状态，别人持锁也不管。"""
    from data_dog.partition_ddl import ddl_guard

    holder = _hold_ddl_lease()
    try:
        monkeypatch.setenv("DD_PARTITION_DDL_LEASE", "0")
        with ddl_guard() as blocker:
            assert blocker is None, "关掉互斥却仍然被挡住"
    finally:
        holder.release()


def _boom_scope(*_a, **_kw):
    raise AssertionError("被互斥挡住时不该打开数据库会话")


def test_startup_ensure_skips_without_opening_a_session_when_blocked(monkeypatch, clean_state):
    """别的副本正在改结构 → 本次启动**跳过补齐**：不开 session、日志说清被谁挡。

    滚动发布时这是常态（N 个副本同时启动），所以它必须是「安静跳过」而不是报错。
    """
    rec = _RecordingLog()
    monkeypatch.setenv("DD_PARTITION_AUTO_ENSURE", "1")
    monkeypatch.setattr(pm, "_log", rec)
    monkeypatch.setattr(pm, "session_scope", _boom_scope)
    holder = _hold_ddl_lease()
    try:
        assert pm.ensure_if_enabled() == []
    finally:
        holder.release()
    assert any("other-replica" in m for m in rec.infos), rec.infos


def test_startup_ensure_releases_the_lease_after_success(monkeypatch, clean_state):
    from data_dog.partition_ddl import ddl_lease_holder

    monkeypatch.setenv("DD_PARTITION_AUTO_ENSURE", "1")
    monkeypatch.setattr(pm, "session_scope", _fake_scope)
    monkeypatch.setattr(pm.partitioning, "is_partitioned",
                        lambda _s, _t=pm.partitioning.PARTITION_TABLE: True)
    monkeypatch.setattr(pm.partitioning, "ensure_partitions", lambda _s, months=3, **_kw: [])
    monkeypatch.setattr(pm, "report_horizon",
                        lambda _s, _t=pm.partitioning.PARTITION_TABLE: None)

    assert pm.ensure_if_enabled() == []
    assert ddl_lease_holder() == "", "启动补齐跑完没释放租约"


def test_startup_ensure_releases_the_lease_even_when_ddl_raises(monkeypatch, clean_state):
    """真错误照抛，但租约**在异常路径上**也必须还回去（否则别的副本等满 TTL）。"""
    from data_dog.partition_ddl import ddl_lease_holder

    def _boom(*_a, **_kw):
        raise RuntimeError("permission denied for schema public")

    monkeypatch.setenv("DD_PARTITION_AUTO_ENSURE", "1")
    monkeypatch.setattr(pm, "session_scope", _fake_scope)
    monkeypatch.setattr(pm.partitioning, "is_partitioned",
                        lambda _s, _t=pm.partitioning.PARTITION_TABLE: True)
    monkeypatch.setattr(pm.partitioning, "ensure_partitions", _boom)
    with pytest.raises(RuntimeError):
        pm.ensure_if_enabled()
    assert ddl_lease_holder() == "", "异常路径没释放租约"
