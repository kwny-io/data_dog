"""启动器（`python -m data_dog.service`）的多 worker 守卫。

为什么单独立这条守卫
-------------------
多 worker 是**真正的水平扩展**（每进程一份解释器 → 绕开 GIL），本机实测也是唯一能
立刻把中负载 P99 压下来的旋钮（`docs/write_path_batching.md` §6）。但它有硬前提：
**有状态的东西不能留在进程内**。限流桶与候选分组若落在 `memory` 后端，多 worker 会
静默各算各的——限流总放行量翻 N 倍、同一段事件被切成 N 段候选。**不会报错，只是
语义错**，所以启动器**拒绝启动**（与全链"拿不准就失败关闭"同口径）。

本文件同时守住"接线"：`main()` 必须真的调用这条守卫，而不是留一个没人调的函数。
"""
from __future__ import annotations

import sys
import types

import pytest

from data_dog.service import __main__ as boot


def _fake_uvicorn(monkeypatch, sink: dict) -> None:
    """把 uvicorn 换成一个只记录调用参数的假模块。"""
    fake = types.ModuleType("uvicorn")

    def _run(*args, **kwargs):
        sink["args"] = args
        sink["kwargs"] = kwargs

    fake.run = _run
    monkeypatch.setitem(sys.modules, "uvicorn", fake)


# ---------------------------------------------------------------- 守卫本身

def test_multi_worker_with_memory_is_refused(monkeypatch):
    """多 worker + 进程内状态后端 → 失败关闭，且错误信息给出可照做的修法。"""
    monkeypatch.setenv("DD_STATE_BACKEND", "memory")
    monkeypatch.delenv("DD_RATE_BACKEND", raising=False)
    with pytest.raises(SystemExit) as ei:
        boot._reject_per_process_state(4)

    msg = str(ei.value)
    assert "DD_SERVICE_WORKERS=4" in msg
    assert "DD_STATE_BACKEND=postgres" in msg, "要给出修法，而不只是报错"


def test_multi_worker_with_shared_state_is_allowed(monkeypatch):
    """状态与限流都落在共享后端 → 多 worker 放行。"""
    monkeypatch.setenv("DD_STATE_BACKEND", "postgres")
    monkeypatch.setenv("DD_RATE_BACKEND", "redis")
    boot._reject_per_process_state(4)          # 不该抛


def test_rate_backend_alone_on_memory_also_refuses(monkeypatch):
    """状态共享但**限流**仍在进程内 → 一样拒绝（限流会各挡各的）。"""
    monkeypatch.setenv("DD_STATE_BACKEND", "postgres")
    monkeypatch.setenv("DD_RATE_BACKEND", "memory")
    with pytest.raises(SystemExit):
        boot._reject_per_process_state(2)


def test_default_auto_backend_on_sqlite_is_treated_as_per_process(monkeypatch):
    """未显式配置 + SQLite → 自动推导出 memory，多 worker 必须被拒。

    这条守的是"没配 `DD_STATE_BACKEND` 时的默认路径"——`_auto_backend()` 只在
    PostgreSQL 连接串下才给 postgres，SQLite 走进程内。
    """
    monkeypatch.delenv("DD_STATE_BACKEND", raising=False)
    monkeypatch.delenv("DD_RATE_BACKEND", raising=False)
    monkeypatch.setenv("DD_DATABASE_URL", "sqlite:///test_guard.db")
    with pytest.raises(SystemExit):
        boot._reject_per_process_state(2)


# ---------------------------------------------------------------- 接线

def test_main_refuses_to_start_multi_worker_with_memory(monkeypatch):
    """`main()` 真的接了这条守卫：拒绝了就不该再把 uvicorn 起起来。"""
    sink: dict = {}
    _fake_uvicorn(monkeypatch, sink)
    monkeypatch.setenv("DD_STATE_BACKEND", "memory")
    monkeypatch.setenv("DD_SERVICE_WORKERS", "2")

    with pytest.raises(SystemExit):
        boot.main()

    assert "kwargs" not in sink, "守卫拒绝了启动，uvicorn 却还是被调用了"


def test_main_passes_worker_count_to_uvicorn(monkeypatch):
    """共享后端下，worker 数要真的透传给 uvicorn（否则这个旋钮是假的）。"""
    sink: dict = {}
    _fake_uvicorn(monkeypatch, sink)
    monkeypatch.setenv("DD_STATE_BACKEND", "postgres")
    monkeypatch.setenv("DD_SERVICE_WORKERS", "4")

    assert boot.main() == 0
    assert sink["kwargs"]["workers"] == 4
    assert sink["args"][0] == "data_dog.service.app:app"
