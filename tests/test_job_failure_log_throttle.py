"""job worker 的**失败日志限频**：日志反映状态变化，不反映次数累加。

背景（2026-10-25 实测）：compose 起服务时库缺表（迁移卡在咨询锁上），worker 每轮都失败，
几分钟打了 **5919 条** `取件失败（共享队列不可用）`。真因（迁移卡住 / `/readyz` 503）
被刷屏完全淹没，现场根本看不出发生了什么。所以：
- 同类失败首次与每 `window_s` 只报一条，并**带累计次数**；
- 恢复时补一条 INFO（说明"恢复前失败过多少次"），而不是静默转好。

**为什么不用 `caplog`**：本仓有多个用例会调用 `setup_logging()`（如
`tests/test_soft_realtime.py`），它会重配根 logger 并关掉传播；于是同一进程里后续用例的
`caplog.records` 恒为空——这条错误断言"日志刷屏"的用例会**假绿/假红**（实测：单独跑 6 绿，
跟在 `test_job_atomicity.py` 后面跑全变红，且记录数恒为 0）。这里改用仓库既有做法：
直接把模块的 `_log` 换成记录器（`tests/test_partition_maintenance.py` 同款），
与全局日志配置彻底解耦。
"""
from __future__ import annotations

import pytest

from data_dog.service import investigation_jobs as ij


class _RecordingLog:
    """最小 `_log` 替身：记账 level/message/extra，不做任何格式化或传播。"""

    def __init__(self) -> None:
        self.records: list[tuple[int, str, dict]] = []

    def _push(self, level: int, msg: str, extra: dict | None, args: tuple = ()) -> None:
        # 真 logging 是**惰性 %-格式化**（`log.info("%s（累计 %d）", a, b)`）。
        # 替身必须做同样的渲染，否则断言只能看到 fmt 串，看不到数字。
        try:
            rendered = msg % args if args else msg
        except Exception:                        # pragma: no cover - 参数不匹配时保留原文
            rendered = msg
        self.records.append((level, rendered, dict(extra or {})))

    def info(self, msg, *a, **kw):                     # noqa: ANN001, ANN002, ANN003
        self._push(20, msg, kw.get("extra"), a)

    def warning(self, msg, *a, **kw):                  # noqa: ANN001, ANN002, ANN003
        self._push(30, msg, kw.get("extra"), a)

    def error(self, msg, *a, **kw):                    # noqa: ANN001, ANN002, ANN003
        self._push(40, msg, kw.get("extra"), a)

    def debug(self, msg, *a, **kw):                    # noqa: ANN001, ANN002, ANN003
        self._push(10, msg, kw.get("extra"), a)

    # 便捷断言口
    def warns(self) -> list[tuple[int, str, dict]]:
        return [r for r in self.records if r[0] == 30]

    def infos(self) -> list[tuple[int, str, dict]]:
        return [r for r in self.records if r[0] == 20]


@pytest.fixture
def log(monkeypatch):
    """把 worker 模块的 `_log` 换成本地记录器（自动还原）。"""
    rec = _RecordingLog()
    monkeypatch.setattr(ij, "_log", rec)
    return rec


@pytest.fixture
def quiet_throttle():
    """把窗口拉长到 1 小时，便于断言"同一窗口内只报一条"。"""
    return ij._FailureThrottle(window_s=3600)


def test_many_failures_in_one_window_log_once_with_count(log, quiet_throttle):
    for _ in range(100):
        quiet_throttle.fail("queue_pop", "取件失败（共享队列不可用）", error="Boom: x")
    warns = log.warns()
    assert len(warns) == 1, f"同因失败刷屏了：{len(warns)} 条"
    assert "累计" in warns[0][1]
    assert warns[0][2]["error"] == "Boom: x"          # 根因不能因为限频而丢


def test_next_window_reports_cumulative_count(monkeypatch, log):
    """第二条限频日志要能说清"上一个窗口以来一共失败了多少次"。"""
    t = {"now": 100.0}
    monkeypatch.setattr(ij.time, "monotonic", lambda: t["now"])
    th = ij._FailureThrottle(window_s=60)
    for _ in range(100):
        th.fail("k", "取件失败")            # 首条立即报（累计 1）
    t["now"] += 61
    th.fail("k", "取件失败")                # 第二窗口：累计应包含前面 100 次
    warns = log.warns()
    assert len(warns) == 2
    assert "101" in warns[1][1], warns[1][1]


def test_next_window_logs_again_with_new_failures(monkeypatch, log):
    t = {"now": 100.0}
    monkeypatch.setattr(ij.time, "monotonic", lambda: t["now"])
    th = ij._FailureThrottle(window_s=60)
    th.fail("k", "取件失败")
    t["now"] += 30
    th.fail("k", "取件失败")               # 窗口内：不报
    t["now"] += 40                          # 累计 70s > 60s：再报
    th.fail("k", "取件失败")
    assert len(log.warns()) == 2


def test_recovery_logs_once_and_resets(log, quiet_throttle):
    quiet_throttle.fail("queue_pop", "取件失败")
    quiet_throttle.fail("queue_pop", "取件失败")
    quiet_throttle.recover("queue_pop", "共享队列已恢复（取件正常）")
    quiet_throttle.recover("queue_pop", "共享队列已恢复（取件正常）")   # 已恢复，不该再报
    infos = log.infos()
    assert len(infos) == 1
    assert "2" in infos[0][1], infos[0][1]


def test_recovery_without_prior_failure_is_silent(log, quiet_throttle):
    quiet_throttle.recover("never_failed", "共享队列已恢复")
    assert not log.records, "没失败过就别打'已恢复'"


class _BrokenQueue:
    """取件永远失败的共享队列（模拟 Redis 不可用 / 表缺失）。"""

    def __init__(self) -> None:
        self.calls = 0

    def pop_batch(self, n=1):                    # noqa: ARG002
        self.calls += 1
        raise RuntimeError('relation "investigation_job" does not exist')

    def push(self, entry):                       # noqa: ARG002
        raise RuntimeError("down")


def test_worker_pop_failure_is_throttled_at_the_real_call_site(log):
    """**回归守卫**：真正会刷屏的那条路径（worker `_pop`）不能每轮打一条。"""
    q = _BrokenQueue()
    runner = ij.JobRunner(lambda **_: {}, workers=1, queue=q, poll_interval_s=0)
    for _ in range(20):
        assert runner._pop(batch_size=1) == []
    warns = log.warns()
    assert q.calls == 20
    assert len(warns) == 1, f"_pop 每次失败都打日志（{len(warns)} 条 / 20 次调用）"
    assert "investigation_job" in str(warns[0][2].get("error", ""))


def test_throttle_does_not_swallow_different_causes(log, quiet_throttle):
    """限频只按**同一个原因键**收敛；不同原因不许被顺带吞掉（否则等于新的沉默）。"""
    quiet_throttle.fail("queue_pop", "取件失败")
    quiet_throttle.fail("recover_pending", "恢复扫描（pending）失败")
    quiet_throttle.fail("recover_running", "恢复扫描（running）失败")
    assert len(log.warns()) == 3, "不同原因被合并了：三种失败只报一条等于丢线索"
