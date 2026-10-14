"""异步 job 的**完成一致性**与**卡死恢复**。

为什么单开一份
--------------
`tests/test_async_investigation.py` 覆盖的是**端点级**主干（提交 → 轮询 → 成功/失败/取消）。
这份盯的是两件以前只写在文档里的事：

1. **报告与 job 终态必须一起落库**。此前 `save_report` 在调查实现里单独提交、
   异常还被吞掉，于是可能出现"job=succeeded **但报告查不到**"——查得到任务、
   查不到产物。修法是把两件事放进**一个事务**（见 `JobRunner._finish`）。
2. **卡死的 job 要能自己回到正轨**：`pending`（取件后/领取前崩溃）→ 重新入队；
   `running`（完成写回前进程被杀）→ 标 `failed(abandoned)`，**不自动重跑**
   （避免"慢但还活着"的 worker 与重跑并发）。

全部离线：真 SQLite 临时库 + 假 `run_fn`，不调 LLM、不起线程。
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from data_dog.service.investigation_jobs import JobRunner, SharedJobQueue
from data_dog.state.memory import InMemoryStateStore


@pytest.fixture()
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    # pending 阈值下限 1s，所以 "1" 真的生效；
    # **running 阈值下限是 10s**（`settings.investigate_stale_running_s` 里的 `max(10.0, ...)`，
    # 防"慢但还活着"被误判），所以这里写 1 实际是 10——种 running 必须用 age_s=30，
    # 用 10 会正好卡在 `started_at < now-10` 的边界上（Windows 时钟粒度 ~15.6ms → 约 20% 概率红）。
    monkeypatch.setenv("DD_INVESTIGATE_STALE_PENDING_S", "1")
    monkeypatch.setenv("DD_INVESTIGATE_STALE_RUNNING_S", "1")
    from data_dog.storage.db import init_db, reset_engine
    reset_engine()
    init_db(url)
    yield url
    reset_engine()


class _Report:
    """最小报告替身（`save_report` 走 `to_dict()`）。"""

    def __init__(self, rid: str = "inv_1") -> None:
        self.investigation_id = rid

    def to_dict(self) -> dict[str, Any]:
        return {"investigation_id": self.investigation_id,
                "disposition": "confirmed", "severity": "critical",
                "confidence": 0.9, "summary": "过热"}


def _seed_job(tenant: str = "t1", job_id: str = "job_1",
              candidate: str = "cand_1", *, status: str = "pending",
              age_s: float = 0.0) -> str:
    from data_dog.storage import models as M
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    with session_scope(tenant_id=tenant) as s:
        repo.create_job(s, tenant_id=tenant, job_id=job_id, candidate_id=candidate)
        if status != "pending" or age_s:
            row = s.get(M.InvestigationJobRow, (tenant, job_id))
            row.status = status
            row.submitted_at = time.time() - age_s
            if status == "running":
                row.started_at = time.time() - age_s
                row.worker_id = "w-old"
    return job_id


def _job(tenant: str = "t1", job_id: str = "job_1") -> dict:
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    with session_scope(tenant_id=tenant) as s:
        return repo.get_job(s, job_id, tenant_id=tenant) or {}


def _report_count(tenant: str = "t1") -> int:
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    with session_scope(tenant_id=tenant) as s:
        return len(repo.list_reports(s, tenant_id=tenant))


def _runner(run_fn, **kw) -> JobRunner:
    return JobRunner(run_fn, workers=1, queue=SharedJobQueue(InMemoryStateStore()),
                     recover_interval_s=999, **kw)


# ---------------------------------------------------------------- 1. 完成一致性

def test_report_and_terminal_state_land_in_one_transaction(db):
    """成功路径：job=succeeded **且**报告在（同事务）。

    注意种子是 `pending`：`_handle` 的**第一步是条件领取**（`pending→running`），
    直接种 `running` 会让领取失败、整条路径根本走不到落终态那一步。
    """
    _seed_job()
    calls: list[str] = []

    def run_fn(tenant_id, candidate_id, should_cancel):
        calls.append(candidate_id)
        return {"status": "succeeded", "report_id": "inv_1",
                "error_code": None, "report": _Report("inv_1")}

    r = _runner(run_fn)
    r._handle({"tenant_id": "t1", "job_id": "job_1", "candidate_id": "cand_1"}, "w1")

    assert calls == ["cand_1"]
    assert _job()["status"] == "succeeded"
    assert _job()["report_id"] == "inv_1"
    assert _report_count() == 1, "报告没落库"


def test_report_write_failure_marks_job_failed_not_succeeded(db, monkeypatch):
    """**核心回归**：报告写不进去时，job 绝不能是 succeeded。

    修前：`save_report` 的异常被吞掉、job 照样标 succeeded → 查得到任务、查不到产物。
    修后：两件事同事务 → 事务回滚 → 再显式标 `failed(persist_failed)`。
    """
    _seed_job()
    from data_dog.storage import repository as repo

    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(repo, "save_report", _boom)

    def run_fn(tenant_id, candidate_id, should_cancel):
        return {"status": "succeeded", "report_id": "inv_1",
                "error_code": None, "report": _Report("inv_1")}

    _runner(run_fn)._handle(
        {"tenant_id": "t1", "job_id": "job_1", "candidate_id": "cand_1"}, "w1")

    job = _job()
    assert job["status"] == "failed", f"报告没写进去却标成了 {job['status']}"
    assert job["error_code"] == "persist_failed"
    assert _report_count() == 0


def test_cancelled_job_is_not_overwritten_and_report_is_not_saved(db):
    """协作式取消的语义：**跑的过程中**被取消 → 终态不覆盖，且不提交产物。

    模拟真实时序：worker 已领取（running）→ 期间收到取消（`cancel_job` 把 running
    置 cancelled）→ worker 仍跑完并返回 succeeded，此时不许改写终态、也不许落报告。
    """
    _seed_job()
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope

    def run_fn(tenant_id, candidate_id, should_cancel):
        with session_scope(tenant_id=tenant_id) as s:      # 模拟"取消先到"
            repo.cancel_job(s, "job_1", tenant_id=tenant_id)
        return {"status": "succeeded", "report_id": "inv_1",
                "error_code": None, "report": _Report("inv_1")}

    _runner(run_fn)._handle(
        {"tenant_id": "t1", "job_id": "job_1", "candidate_id": "cand_1"}, "w1")

    assert _job()["status"] == "cancelled", "终态被覆盖了"
    assert _report_count() == 0, "取消后跑完的产物不该落库"


def test_failed_job_still_records_error_code(db):
    _seed_job()

    def run_fn(tenant_id, candidate_id, should_cancel):
        return {"status": "failed", "report_id": None,
                "error_code": "unavailable", "report": None}

    _runner(run_fn)._handle(
        {"tenant_id": "t1", "job_id": "job_1", "candidate_id": "cand_1"}, "w1")
    assert _job()["status"] == "failed"
    assert _job()["error_code"] == "unavailable"
    assert _report_count() == 0


# ---------------------------------------------------------------- 2. 卡死恢复

def test_stale_pending_job_is_requeued_and_executed_once(db):
    """`pending` 过旧（取件后/领取前崩溃）→ 重新入队，且**只执行一次**。"""
    _seed_job(status="pending", age_s=10)
    calls: list[str] = []

    def run_fn(tenant_id, candidate_id, should_cancel):
        calls.append(candidate_id)
        return {"status": "succeeded", "report_id": "inv_1", "error_code": None,
                "report": _Report("inv_1")}

    r = _runner(run_fn)
    stats = r.recover_stale()
    assert stats["requeued"] == 1

    batch = r._queue.pop_batch(10)
    assert len(batch) == 1 and batch[0]["job_id"] == "job_1"

    # 条件领取保证：即使队列里出现同一条两次，也只有一个能真正开跑
    r._handle(batch[0], "w1")
    r._handle(batch[0], "w2")
    assert calls == ["cand_1"], f"同一 job 被执行了多次：{calls}"
    assert _job()["status"] == "succeeded"


def test_stale_running_job_is_abandoned_and_not_rerun(db):
    """`running` 过旧（完成写回前被杀）→ 标 failed(abandoned)，**不入队重跑**。"""
    _seed_job(status="running", age_s=30)
    r = _runner(lambda *a: {"status": "succeeded"})

    stats = r.recover_stale()
    assert stats["abandoned"] == 1
    assert stats["requeued"] == 0
    job = _job()
    assert job["status"] == "failed" and job["error_code"] == "abandoned"
    assert r._queue.pop_batch(10) == [], "僵死 job 不该被重新入队（避免并发重跑）"


def test_recover_ignores_fresh_jobs(db):
    """刚提交/刚开跑的 job 不能被误判（阈值的作用就是区分"卡住"与"正常"）。"""
    _seed_job("t1", "job_fresh", status="pending", age_s=0)
    _seed_job("t1", "job_running", status="running", age_s=0)
    r = _runner(lambda *a: {"status": "succeeded"})

    assert r.recover_stale() == {"requeued": 0, "abandoned": 0, "errors": 0}
    assert _job("t1", "job_fresh")["status"] == "pending"
    assert _job("t1", "job_running")["status"] == "running"


def test_running_stale_threshold_has_a_10s_floor(monkeypatch):
    """`running` 阈值有 **10s 下限**：env 设成 1 实际仍是 10（防误判"慢但还活着"的 worker）。

    这条钉死一个真实踩过的坑：测试把 env 设成 `1` 就以为阈值是 1，于是用 `age_s=10` 种 running，
    结果正好落在 `started_at < now - 10` 的边界上——Windows `time.time()` 粒度 ~15.6ms，
    全量跑约 20% 概率红（单独跑常过）。要么种更老（30s），要么别依赖下限之下的值。
    """
    from data_dog import settings

    monkeypatch.setenv("DD_INVESTIGATE_STALE_RUNNING_S", "1")
    assert settings.investigate_stale_running_s() == 10.0
    monkeypatch.setenv("DD_INVESTIGATE_STALE_RUNNING_S", "30")
    assert settings.investigate_stale_running_s() == 30.0
    monkeypatch.setenv("DD_INVESTIGATE_STALE_PENDING_S", "1")
    assert settings.investigate_stale_pending_s() == 1.0, "pending 没有 10s 下限"


def test_recover_is_multi_replica_safe(db):
    """两个副本同时扫：入队幂等、终态是条件更新（第二次不再改）。"""
    _seed_job("t1", "job_p", status="pending", age_s=10)
    _seed_job("t1", "job_r", status="running", age_s=30)

    r1 = _runner(lambda *a: {"status": "succeeded"})
    r2 = _runner(lambda *a: {"status": "succeeded"})
    s1 = r1.recover_stale()
    s2 = r2.recover_stale()

    assert s1["requeued"] == 1 and s2["requeued"] == 1   # 队列里两条，但只会执行一次
    assert s1["abandoned"] == 1
    assert s2["abandoned"] == 0, "第二次扫时它已是终态，不该再改"
    assert _job("t1", "job_r")["status"] == "failed"


def test_recover_scan_uses_explicit_system_scope(db, monkeypatch):
    """跨租户扫描必须走 `system=True`：否则 RLS 下**零行可见**（静默不恢复）。

    这条把"扫描用了系统通道"变成可断言的事实，避免有人"顺手"把它改成普通会话
    ——那在生产 PG 上会让恢复扫描与升级扫描一起静默失效。
    """
    from data_dog.storage import db as dbmod

    seen: list[tuple[bool, str | None]] = []
    real = dbmod.session_scope

    def _spy(url=None, *, tenant_id=None, system=False):
        seen.append((bool(system), tenant_id))
        return real(url, tenant_id=tenant_id, system=system)

    monkeypatch.setattr(dbmod, "session_scope", _spy)
    _seed_job("t1", "job_p", status="pending", age_s=10)
    _seed_job("t1", "job_r", status="running", age_s=30)
    seen.clear()                      # 只看扫描期间

    _runner(lambda *a: {"status": "succeeded"}).recover_stale()

    assert (True, None) in seen, "跨租户扫描没走 system=True"
    # 逐条处理仍带租户（不是"系统通道一把梭"）
    assert (False, "t1") in seen
