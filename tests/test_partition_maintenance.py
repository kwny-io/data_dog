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

    def warning(self, msg, *args, **_kw):
        self.warnings.append(msg % args if args else msg)

    def info(self, msg, *args, **_kw):
        return None


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
