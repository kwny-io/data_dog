"""多副本一致性告警必须真的会响（`service/app.py:_warn_if_shared_state_unavailable`）。

为什么补这个测试：这条告警是**静默错误**的唯一出口——
多副本 + memory 状态后端时，限流和候选合并在各副本各算各的，**不会崩、不会报错、数字只是不对**。
如果这段代码哪天被重构掉、或 `isinstance` 判断失效，线上没有任何信号。
仓库此前**没有任何测试**断言它会响（`grep state_backend_memory tests/` 为空）。

为什么不用 `caplog`：本仓多个用例会调用 `setup_logging()`（改全局 handler/propagate），
`caplog.records` 可能恒为空 → 「单独跑绿、全量跑红」。改用记录桩直接替掉模块 logger，
与全局日志配置无关（同 `tests/test_partition_maintenance.py` 的 `_RecordingLog` 写法）。
"""
from __future__ import annotations

import sys

# 必须从 sys.modules 取真模块：`data_dog/service/__init__.py` 里把名字 `app` 重新导出成了
# FastAPI 实例，所以 `from data_dog.service import app` 拿到的是 **应用对象**，不是模块
# （直接 setattr 会报 `FastAPI object has no attribute '_log'`）。
from data_dog.service.app import create_app  # noqa: F401  （导入以满足前置条件）

app_mod = sys.modules["data_dog.service.app"]


class _RecordingLog:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.extras: list[dict] = []

    def warning(self, msg, *args, **kw):
        self.warnings.append(msg % args if args else msg)
        self.extras.append(kw.get("extra") or {})


def _run(monkeypatch, store):
    rec = _RecordingLog()
    monkeypatch.setattr(app_mod, "_log", rec)
    monkeypatch.setattr(app_mod, "get_state_store", lambda: store)
    app_mod._warn_if_shared_state_unavailable()
    return rec


def test_memory_backend_warns_with_actionable_hint(monkeypatch):
    """memory 后端 → 必须告警，且文案里给出可执行的下一步（配 postgres/redis）。"""
    from data_dog.state.memory import InMemoryStateStore

    rec = _run(monkeypatch, InMemoryStateStore())
    assert len(rec.warnings) == 1, f"应恰好告警一次，实际 {rec.warnings}"
    msg = rec.warnings[0]
    assert "memory" in msg
    assert "DD_STATE_BACKEND" in msg, "告警必须告诉运维怎么改，否则等于没说"
    assert rec.extras[0].get("migration") == "state_backend_memory"


def test_shared_backend_stays_silent(monkeypatch):
    """非 memory 后端 → 不许告警（否则日志会被刷屏，真问题淹没）。"""

    class _Shared:
        pass

    rec = _run(monkeypatch, _Shared())
    assert rec.warnings == []


def test_store_construction_failure_does_not_crash(monkeypatch):
    """后端构建失败时**静默返回**，把结论交给 /readyz，不要在这里二次崩溃。"""

    def _boom():
        raise RuntimeError("backend down")

    rec = _RecordingLog()
    monkeypatch.setattr(app_mod, "_log", rec)
    monkeypatch.setattr(app_mod, "get_state_store", _boom)
    app_mod._warn_if_shared_state_unavailable()   # 不抛即通过
    assert rec.warnings == []
