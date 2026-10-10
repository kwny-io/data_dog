"""数据保留与清理作业（④）：把 `docs/slo.md` §6 声明的保留期**真的执行掉**。

要守住的四件事
-------------
1. **截止时间语义**：只删 `时间列 < now - days*86400` 的行，且用的是 **ingestion 时间**
   （`created_at`/`ts`/`finished_at`），不是事件时间——否则一条补传的历史时间戳会让这行
   永远"年轻"、永远不被清掉。
2. **破坏性动作默认关闭**：原始日志/派生候选/已终态 job 默认执行；**告警台账与安全审计
   默认 0 = 不删**，必须显式开启。审计"只增不改"是它的全部价值。
3. **有界批次**：批次数用满就停并**如实标记 `capped`**，而不是"为了清完把库锁死"
   （本项目已经在 P99 上吃过"长事务排队"的亏）。
4. **跨租户必须走系统通道**：清理是系统级动作，PG 上启用 RLS 后，不带系统扫描标志的
   会话**只看得见零行** → 会静默变成"啥也没删"。这里显式断言 `system=True`。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select

from data_dog import audit, retention
from data_dog.storage.db import init_db, reset_engine
from data_dog.storage.models import Base

NOW = 1_800_000_000.0
DAY = 86400.0


@pytest.fixture()
def url(tmp_path, monkeypatch):
    u = f"sqlite:///{(tmp_path / 'retention.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", u)
    reset_engine()
    init_db(u)
    yield u
    reset_engine()


def _seed(u: str, table: str, rows: list[dict]) -> None:
    eng = create_engine(u)
    with eng.begin() as c:
        c.execute(Base.metadata.tables[table].insert(), rows)
    eng.dispose()


def _count(u: str, table: str) -> int:
    eng = create_engine(u)
    with eng.connect() as c:
        n = c.execute(select(func.count()).select_from(
            Base.metadata.tables[table])).scalar()
    eng.dispose()
    return int(n or 0)


def _old(n: int, *, tenant: str = "t1", prefix: str = "old") -> list[dict]:
    """n 条"很久以前写入"的遥测行（默认保留 7 天 → 必被清）。"""
    return [{"tenant_id": tenant, "event_id": f"{prefix}-{i}", "device_sn": "SN1",
             "occurred_at": "2026-01-01T00:00:00Z", "component_ref": "leg.hip",
             "created_at": NOW - 30 * DAY} for i in range(n)]


def _recent(n: int, *, tenant: str = "t1") -> list[dict]:
    return [{"tenant_id": tenant, "event_id": f"new-{i}", "device_sn": "SN1",
             "occurred_at": "2026-01-01T00:00:00Z", "component_ref": "leg.hip",
             "created_at": NOW - 1 * DAY} for i in range(n)]


def _row(report: dict, table: str) -> dict:
    return next(r for r in report["tables"] if r["table"] == table)


def _audit_ids(u: str) -> list[str]:
    eng = create_engine(u)
    with eng.connect() as c:
        ids = [r[0] for r in c.execute(select(
            Base.metadata.tables["audit_log"].c.audit_id)).all()]
    eng.dispose()
    return sorted(ids)


# ---------------------------------------------------------------- 截止时间

def test_purges_only_rows_older_than_cutoff(url):
    """7 天保留：30 天前的删掉，1 天前的留下。"""
    _seed(url, "telemetry_events", _old(3) + _recent(2))
    assert _count(url, "telemetry_events") == 5

    report = retention.run_once(now=NOW)

    assert _count(url, "telemetry_events") == 2
    assert _row(report, "telemetry_events")["deleted"] == 3


def test_uses_ingestion_time_not_event_time(url):
    """保留按 `created_at`（写入时刻）判，**不是** `occurred_at`。

    伪造/补传一条 `occurred_at` 很老的行不该因此被清；反过来，`occurred_at` 很新但
    写入很久的行**必须**被清——否则"保留 N 天"可以被攻击者用时间戳绕过（存证堆积）。
    """
    _seed(url, "telemetry_events", [{
        "tenant_id": "t1", "event_id": "ancient-event-time",
        "device_sn": "SN1", "occurred_at": "1999-01-01T00:00:00Z",
        "component_ref": "leg.hip", "created_at": NOW - 1 * DAY}, {
        "tenant_id": "t1", "event_id": "fresh-event-time",
        "device_sn": "SN1", "occurred_at": "2026-12-31T23:59:59Z",
        "component_ref": "leg.hip", "created_at": NOW - 30 * DAY}])

    retention.run_once(now=NOW)

    eng = create_engine(url)
    with eng.connect() as c:
        left = [r[0] for r in c.execute(select(
            Base.metadata.tables["telemetry_events"].c.event_id)).all()]
    eng.dispose()
    assert left == ["ancient-event-time"], (
        "按事件时间判了：老的 occurred_at 反而被留下 / 新的 occurred_at 反而被删")


# ---------------------------------------------------------------- 默认关闭

def test_zero_days_disables_that_table(url, monkeypatch):
    """`DD_RETENTION_RAW_DAYS=0` → 这张表**一行都不动**（0 = 关闭，不是"立刻全删"）。"""
    monkeypatch.setenv("DD_RETENTION_RAW_DAYS", "0")
    _seed(url, "telemetry_events", _old(3))

    report = retention.run_once(now=NOW)

    assert _count(url, "telemetry_events") == 3
    row = _row(report, "telemetry_events")
    assert row["off"] is True and row["deleted"] == 0


def test_audit_is_off_by_default_and_purges_only_when_enabled(url, monkeypatch):
    """安全审计**默认不删**；显式设 `DD_RETENTION_AUDIT_DAYS` 才清。

    销毁审计记录本身就是合规风险，所以它不能有"默认就删"的默认值。

    注意断言的是**到期那条**没了，而不是"表空了"：清理动作自己会往 `audit_log` 写一条
    `retention.purge`（"销毁也要留痕"，见 ① 的设计）。所以清完之后表里**必然**还有
    一条——这正是我们想要的。
    """
    _seed(url, "audit_log", [{
        "tenant_id": "t1", "audit_id": "aud-old", "ts": NOW - 400 * DAY,
        "action": "auth.denied", "outcome": "denied"}])

    retention.run_once(now=NOW)
    assert _audit_ids(url) == ["aud-old"], "默认就把审计删了——这不该发生"

    monkeypatch.setenv("DD_RETENTION_AUDIT_DAYS", "365")
    report = retention.run_once(now=NOW)
    assert _row(report, "audit_log")["deleted"] == 1

    left = _audit_ids(url)
    assert "aud-old" not in left, "到期的审计记录没被清掉"
    # 剩下的那条是本次清理"自己留的痕"——销毁必须可查
    assert len(left) == 1
    assert left[0] != "aud-old"


def test_alerts_are_off_by_default(url, monkeypatch):
    monkeypatch.delenv("DD_RETENTION_ALERT_DAYS", raising=False)
    assert retention.cutoffs(now=NOW).get("alerts") is None


# ---------------------------------------------------------------- 只清终态

def test_pending_jobs_are_never_purged_even_if_ancient(url):
    """未终态的 job 再老也不删——那是**还没干完的活**。"""
    _seed(url, "investigation_job", [
        {"tenant_id": "t1", "job_id": "done", "candidate_id": "c1",
         "status": "succeeded", "submitted_at": NOW - 90 * DAY,
         "finished_at": NOW - 90 * DAY},
        {"tenant_id": "t1", "job_id": "running", "candidate_id": "c2",
         "status": "running", "submitted_at": NOW - 90 * DAY,
         "finished_at": None},
    ])

    report = retention.run_once(now=NOW)

    assert _row(report, "investigation_job")["deleted"] == 1
    eng = create_engine(url)
    with eng.connect() as c:
        left = [r[0] for r in c.execute(select(
            Base.metadata.tables["investigation_job"].c.job_id)).all()]
    eng.dispose()
    assert left == ["running"], f"未终态 job 被清掉了：{left}"


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "abandoned"])
def test_all_terminal_statuses_are_purged(url, status):
    """终态闭集全覆盖（漏一个就会在那类 job 上无限堆积）。"""
    _seed(url, "investigation_job", [
        {"tenant_id": "t1", "job_id": f"j-{status}", "candidate_id": "c",
         "status": status, "submitted_at": NOW - 90 * DAY,
         "finished_at": NOW - 90 * DAY}])
    retention.run_once(now=NOW)
    assert _count(url, "investigation_job") == 0


# ---------------------------------------------------------------- 有界批次

def test_batches_are_bounded_and_capped_is_reported(url):
    """5 条待清、每批 2 条、最多 2 批 → 本轮只删 4 条，并**如实标记 capped**。

    这一条守的是"宁可没清完，也不把库锁死"：批数用满就停，剩下的下一轮继续。
    """
    _seed(url, "telemetry_events", _old(5))

    report = retention.run_once(now=NOW, batch_size=2, max_batches=2)

    row = _row(report, "telemetry_events")
    assert row["deleted"] == 4, row
    assert row["batches"] == 2
    assert row["capped"] is True, "到批数上限却没标记——运维会以为已经清完了"
    assert _count(url, "telemetry_events") == 1


def test_not_capped_when_drained(url):
    _seed(url, "telemetry_events", _old(3))
    report = retention.run_once(now=NOW, batch_size=10, max_batches=10)
    row = _row(report, "telemetry_events")
    assert (row["deleted"], row["capped"]) == (3, False)


@pytest.mark.parametrize("kw", [{"batch_size": 0}, {"max_batches": 0},
                                {"batch_size": -1}])
def test_invalid_batch_params_are_rejected(url, kw):
    with pytest.raises(ValueError):
        retention.run_once(now=NOW, **kw)


# ---------------------------------------------------------------- dry-run / 留痕

def test_dry_run_deletes_nothing_and_writes_no_audit(url):
    _seed(url, "telemetry_events", _old(3))

    report = retention.run_once(now=NOW, dry_run=True)

    assert _count(url, "telemetry_events") == 3
    assert report["dry_run"] is True
    assert _row(report, "telemetry_events")["would_delete"] == 3
    assert report["total"] == 3
    rows = audit.query(tenant_id=None, action=audit.ACTION_RETENTION_PURGE)["items"]
    assert not rows, "空跑不该留痕（周期性任务会把审计刷成噪声）"


def test_purge_is_audited_with_counts(url):
    """真的删了就要留痕：删了什么、各表多少行——"销毁"也要可查。"""
    _seed(url, "telemetry_events", _old(3))
    retention.run_once(now=NOW)

    rows = audit.query(tenant_id=None, action=audit.ACTION_RETENTION_PURGE)["items"]
    assert rows, "删了东西却没有审计记录"
    detail = rows[0]["detail"]
    assert detail["total_deleted"] == 3
    assert any(t["table"] == "telemetry_events" and t["deleted"] == 3
               for t in detail["tables"])


def test_plan_is_read_only(url):
    _seed(url, "telemetry_events", _old(4) + _recent(1))

    planned = retention.plan(now=NOW)

    assert _count(url, "telemetry_events") == 5, "plan() 竟然删了东西"
    row = next(p for p in planned if p["table"] == "telemetry_events")
    assert row["would_delete"] == 4
    assert row["days"] == 7


# ---------------------------------------------------------------- RLS 通道

def test_purge_uses_system_scope(url, monkeypatch):
    """跨租户清理必须走 `session_scope(system=True)`。

    PG 上启用 RLS 后，不带系统扫描标志的会话**只看得见零行** → 清理会静默变成
    "啥也没删"（正是迁移 `b7c8d9e0f1a2` 修的那类静默失效）。这条把它钉住：
    一旦有人把 `system=True` 去掉，这里直接红，而不是等到生产上"保留策略不生效"。
    """
    from data_dog.storage import db as db_mod

    seen: list[dict] = []
    real = db_mod.session_scope

    def spy(*a, **kw):
        seen.append(kw)
        return real(*a, **kw)

    monkeypatch.setattr(db_mod, "session_scope", spy)
    _seed(url, "telemetry_events", _old(1))

    retention.run_once(now=NOW)

    assert seen, "没有调用 session_scope"
    # 注意：`audit.record` 自己也会开一个会话，且它是**租户作用域**的
    # （把 `retention.purge` 记在保留租户 `-` 名下），所以这里不能要求"全部"都是
    # 系统通道。要钉的是**删除那一步**：它是第一个会话，且必须是系统通道。
    assert seen[0].get("system") is True, (
        f"清理没走系统通道，RLS 下会静默删 0 行：{seen}")


def test_cutoffs_skip_disabled_knobs(monkeypatch):
    monkeypatch.setenv("DD_RETENTION_ALERT_DAYS", "0")
    monkeypatch.setenv("DD_RETENTION_AUDIT_DAYS", "0")
    c = retention.cutoffs(now=NOW)
    assert "alerts" not in c and "audit_log" not in c
    assert c["telemetry_events"] == pytest.approx(NOW - 7 * DAY)
