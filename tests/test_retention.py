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
    """插入种子行。

    `telemetry_events` 的主键含**摄取日桶** `created_day`（A9，= floor(created_at/86400)）：
    直接构造行时它必须显式给出，否则 NOT NULL 主键会 IntegrityError。这里按 created_at 补算，
    让种子行只写业务字段。
    """
    if table == "telemetry_events":
        rows = [{**r, "created_day": int(r["created_at"] // DAY)} for r in rows]
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


# ---------------------------------------------------------------- 单持有者租约（多副本）

@pytest.fixture()
def clean_state():
    """每个用例一把干净的共享状态：租约存在进程内单例里，用例之间不能互相串。"""
    from data_dog.state import reset_state_store

    reset_state_store()
    yield
    reset_state_store()


def _hold_purge_lease(*, owner: str = "other-replica", ttl_s: float = 300.0):
    """模拟「另一个副本正在清」：以别的 owner 抢住同一把租约。"""
    from data_dog.state import Lease, get_state_store

    holder = Lease(get_state_store(), retention._PURGE_LEASE_KEY, owner,
                   ttl_s=ttl_s)
    assert holder.acquire(), "测试自己都没抢到租约"
    return holder


def test_run_once_skips_when_another_replica_holds_the_lease(url, clean_state):
    """别的副本持有租约 → 本轮**跳过且一行不删**，并如实标记 skipped。

    钉住的是「跳过」与「删了 0 行」必须能区分：前者是「这轮不该我干」，后者是「没有
    东西可清」。混在一起，运维看报告就分不出「清理根本没跑起来」和「库里很干净」。
    """
    _seed(url, "telemetry_events", _old(3))
    holder = _hold_purge_lease()
    try:
        report = retention.run_once(now=NOW)
    finally:
        holder.release()

    assert report["skipped"] == "lease_held", report
    assert report["lease_owner"] == "other-replica"
    assert report["total"] == 0 and report["tables"] == []
    assert _count(url, "telemetry_events") == 3, "抢不到租约竟然还是删了"


def test_run_once_proceeds_after_the_holder_releases(url, clean_state):
    """接管有窗口但不丢活：持有者一放，下一轮就接着清。"""
    _seed(url, "telemetry_events", _old(3))
    holder = _hold_purge_lease()

    assert retention.run_once(now=NOW)["skipped"] == "lease_held"
    holder.release()

    report = retention.run_once(now=NOW)
    assert report.get("skipped") is None
    assert report["total"] == 3
    assert _count(url, "telemetry_events") == 0


def test_lease_is_released_so_the_next_run_can_take_it(url, clean_state):
    """干完就还锁：单副本连续两轮都能跑（没人永远占着），第二轮无事可做。"""
    from data_dog.state import Lease, get_state_store

    _seed(url, "telemetry_events", _old(2))
    assert retention.run_once(now=NOW)["total"] == 2
    assert retention.run_once(now=NOW)["total"] == 0
    idle = Lease(get_state_store(), retention._PURGE_LEASE_KEY, "probe")
    assert idle.holder() == "", "清理跑完没有释放租约——别的副本会被永久挡住"


def test_lease_off_switch_bypasses_the_lock(url, clean_state, monkeypatch):
    """环境变量关掉租约：单机直跑不抢锁，别人持有也不管。

    给「只有一个副本、不想多一次共享状态往返」的场景留的退路——但多副本下必须开。
    """
    _seed(url, "telemetry_events", _old(3))
    holder = _hold_purge_lease()
    try:
        monkeypatch.setenv("DD_RETENTION_LEASE", "0")
        report = retention.run_once(now=NOW)
        assert report.get("skipped") is None, "关掉租约却仍然跳过"
        assert report["total"] == 3
        assert _count(url, "telemetry_events") == 0
    finally:
        holder.release()


def test_dry_run_neither_takes_nor_respects_the_lease(url, clean_state):
    """只读动作不参与互斥：dry-run 不抢锁，别人持锁也照常统计。"""
    from data_dog.state import Lease, get_state_store

    _seed(url, "telemetry_events", _old(2))
    report = retention.run_once(now=NOW, dry_run=True)
    assert report["total"] == 2 and "skipped" not in report
    idle = Lease(get_state_store(), retention._PURGE_LEASE_KEY, "probe")
    assert idle.holder() == "", "dry-run 竟然留了锁"

    holder = _hold_purge_lease()
    try:
        again = retention.run_once(now=NOW, dry_run=True)
    finally:
        holder.release()
    assert again["total"] == 2 and "skipped" not in again
    assert _count(url, "telemetry_events") == 2, "dry-run 竟然删了东西"
