"""升级 sweeper 的**单持有者租约**：多副本不许重复升级（不许重复打扰值班人）。

为什么单开一份
--------------
`tests/test_escalation.py` 覆盖"到期就升级、ack 就不升、到顶就 failed"的单副本语义。
这份盯的是**多副本**：两个副本各自扫同一张 `alert_delivery` 表时，会**同时**看到同一条
到期行并各自走 `_escalate_row` → 各自 `registry.send()`。台账按
`delivery_id = hash(event_id, level)` 去重，但**通知已经发出去了**——
所以"升级多少次"必须直接数**发送次数**，不能只看台账行数。

租约本体（acquire/renew/过期抢占/释放）的正反用例在 `tests/test_lease.py`；
这里只验它在 sweeper 上的接线与语义。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from data_dog.alerting.escalation import (
    EscalationEngine,
    EscalationSweeper,
    make_delivery_id,
)
from data_dog.alerting.lifecycle import AlertEvent
from data_dog.alerting.oncall import make_rota_id
from data_dog.state.lease import Lease
from data_dog.state.memory import InMemoryStateStore

LEASE_KEY = "alert:escalation:sweeper"


# ---------------------------------------------------------------- 测试脚手架

class _Clock:
    """可手动推进的墙钟（租约用它比较过期时间）。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class _CountingRegistry:
    """只数"发出去了几次"，不真的投递（通知才是重复升级的可见后果）。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, note, *, route):                    # noqa: ANN001 - 与协议同形
        self.sent.append((str(route.channel), str(route.assignee)))
        return SimpleNamespace(ok=True, detail="", receipt="fake")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'sweep.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    monkeypatch.setenv("DD_NOTIFY_MAX_LEVEL", "3")
    from data_dog.storage.db import init_db, reset_engine
    reset_engine()
    init_db(url)
    yield url
    reset_engine()


def _seed_rota(tenant: str, *, level: int, assignee: str,
               domain: str = "joint_motor") -> None:
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    now = time.time()
    with session_scope(tenant_id=tenant) as s:
        repo.upsert_rota(s, tenant_id=tenant,
                         rota_id=make_rota_id(domain, level, now - 60),
                         domain=domain, level=level, assignee=assignee,
                         start_at=now - 60, end_at=now + 3600,
                         channel="webhook", target="http://127.0.0.1:1/x")


def _seed_alert(tenant: str, event_id: str = "alert_x") -> None:
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    ev = AlertEvent(event_id=event_id, idempotency_key=f"k-{event_id}",
                    device_ref="device-abc", component_ref="joint_motor.front_hip",
                    event_type="overheat", state="confirmed",
                    severity="critical", summary="过热")
    with session_scope(tenant_id=tenant) as s:
        repo.save_alert(s, ev, tenant_id=tenant)


def _alert(tenant: str, event_id: str = "alert_x") -> dict:
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    with session_scope(tenant_id=tenant) as s:
        return repo.get_alert(s, event_id, tenant_id=tenant) or {}


def _expire_deadlines(tenant: str, *, delta: float = 5.0) -> None:
    from data_dog.storage import models as M
    from data_dog.storage.db import session_scope
    with session_scope(tenant_id=tenant) as s:
        for r in s.query(M.AlertDeliveryRow).filter(
                M.AlertDeliveryRow.tenant_id == tenant,
                M.AlertDeliveryRow.status == "pending").all():
            r.ack_deadline = time.time() - delta


def _levels(tenant: str) -> list[tuple[int, str]]:
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    with session_scope(tenant_id=tenant) as s:
        return [(int(r["level"]), str(r["status"]))
                for r in repo.list_deliveries(s, tenant_id=tenant)]


def _setup_two_levels(registry: _CountingRegistry,
                      tenant: str = "tenant-a") -> EscalationEngine:
    """种 L1/L2 值班 + 一条 confirmed 告警，并派发 L1（产生一条 pending 投递）。"""
    _seed_rota(tenant, level=1, assignee="alice")
    _seed_rota(tenant, level=2, assignee="bob")
    _seed_alert(tenant)
    eng = EscalationEngine(registry=registry)
    eng.dispatch(tenant_id=tenant, alert=_alert(tenant))
    _expire_deadlines(tenant)
    return eng


# ---------------------------------------------------------------- 核心：不重复升级

def test_only_the_lease_holder_sweeps(db):
    """两副本同时扫：**只有一个**发出升级通知，台账只多一条 L2。"""
    reg = _CountingRegistry()
    eng = _setup_two_levels(reg)
    assert len(reg.sent) == 1                       # dispatch 的 L1

    store = InMemoryStateStore()                   # 共享的租约状态
    a = EscalationSweeper(eng, interval_s=15,
                          lease=Lease(store, LEASE_KEY, "replica-a"))
    b = EscalationSweeper(eng, interval_s=15,
                          lease=Lease(store, LEASE_KEY, "replica-b"))

    stats_a = a.sweep_tick()
    stats_b = b.sweep_tick()                       # 同一轮，另一个副本

    assert stats_a is not None and stats_a["escalated"] == 1
    assert stats_b is None, "非持有者不该扫"
    assert len(reg.sent) == 2, f"值班人被重复打扰：{reg.sent}"
    assert sorted(_levels("tenant-a")) == [(1, "escalated"), (2, "pending")]
    assert b.skipped_ticks == 1
    assert a.holder == "replica-a"


def test_handover_after_release_is_immediate(db):
    """持有者停止（主动释放）→ 另一个副本**立刻**接管，不必等 TTL。"""
    reg = _CountingRegistry()
    eng = _setup_two_levels(reg)
    store = InMemoryStateStore()
    a = EscalationSweeper(eng, lease=Lease(store, LEASE_KEY, "replica-a"))
    b = EscalationSweeper(eng, lease=Lease(store, LEASE_KEY, "replica-b"))

    assert a.sweep_tick() is not None              # a 接管并升到 L2
    assert b.sweep_tick() is None                  # b 拿不到

    _expire_deadlines("tenant-a")                  # L2 也超时，待升级
    a.stop()                                       # 释放租约
    assert b.sweep_tick() is not None, "释放后应立刻可接管"
    assert b.holder == "replica-b"
    assert (2, "escalated") in _levels("tenant-a")


def test_handover_after_ttl_expiry(db):
    """持有者崩了（不续约）→ TTL 到期后可被抢占。"""
    reg = _CountingRegistry()
    eng = _setup_two_levels(reg)
    store = InMemoryStateStore()
    clk = _Clock()
    a = EscalationSweeper(eng, lease=Lease(store, LEASE_KEY, "replica-a",
                                           ttl_s=30, clock=clk))
    b = EscalationSweeper(eng, lease=Lease(store, LEASE_KEY, "replica-b",
                                           ttl_s=30, clock=clk))

    assert a.sweep_tick() is not None
    assert b.sweep_tick() is None
    clk.advance(31)                                # a 不再续约 → 过期
    _expire_deadlines("tenant-a")
    assert b.sweep_tick() is not None, "TTL 过期后应可接管"
    assert b.holder == "replica-b"


def test_readyz_reports_escalation_lease_holder(tmp_path, monkeypatch):
    """`/readyz` 要能把"谁在负责升级扫描"报出来（多副本排障），且**不影响就绪**。"""
    from fastapi.testclient import TestClient

    url = f"sqlite:///{(tmp_path / 'rz.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    monkeypatch.setenv("DD_NOTIFY_ENABLED", "1")
    monkeypatch.setenv("DD_NOTIFY_SWEEP_INTERVAL_S", "1")
    from data_dog.storage.db import init_db, reset_engine
    reset_engine()
    init_db(url)
    try:
        from data_dog.service.app import create_app
        with TestClient(create_app()) as c:
            res = c.get("/readyz")
            body = res.json()
    finally:
        reset_engine()

    assert res.status_code == 200
    esc = body["checks"]["escalation"]
    assert esc["ok"] is True
    assert esc["lease_holder"], "单副本时应由自己持有租约"
    assert esc["skipped_ticks"] == 0
    # 租约 TTL 必须显著大于扫描间隔（否则两轮之间就过期，别的副本会插进来）
    assert esc["lease_ttl_s"] >= 3.0 * esc["interval_s"]


def test_lease_unavailable_fails_closed(db):
    """租约后端不可用 → **本轮不扫**（宁可不升级，也不冒重复打扰的风险）。"""
    reg = _CountingRegistry()
    eng = _setup_two_levels(reg)

    class _BrokenStore(InMemoryStateStore):
        def mutate(self, key, fn):                 # noqa: ANN001
            raise RuntimeError("state store down")

    sweeper = EscalationSweeper(
        eng, lease=Lease(_BrokenStore(), LEASE_KEY, "replica-a"))

    assert sweeper.sweep_tick() is None
    assert sweeper.skipped_ticks == 1
    assert len(reg.sent) == 1, "失败关闭：不该发出任何升级通知"
    assert sorted(_levels("tenant-a")) == [(1, "pending")], "到期行仍留在库里，下轮可继续"


def test_without_lease_it_still_sweeps(db):
    """显式不要租约（单副本/测试）→ 行为与从前一致。"""
    reg = _CountingRegistry()
    eng = _setup_two_levels(reg)
    sweeper = EscalationSweeper(eng, lease=False)

    stats = sweeper.sweep_tick()
    assert stats is not None and stats["escalated"] == 1
    assert len(reg.sent) == 2
    assert sweeper.holder == ""


def test_default_lease_is_built_from_shared_store(db, monkeypatch):
    """不传 lease → 自动建在**共享状态**上（与限流/候选归并同一条多副本边界）。"""
    from data_dog import settings
    from data_dog.state import factory

    factory.reset_state_store()
    try:
        sweeper = EscalationSweeper(EscalationEngine(registry=_CountingRegistry()))
        assert sweeper._lease is not None
        assert sweeper._lease.key == LEASE_KEY
        # TTL 必须显著大于扫描间隔，否则两轮之间就过期、别的副本会插进来扫
        interval = settings.notify_sweep_interval_s()
        assert sweeper._lease._ttl >= 3.0 * interval
    finally:
        factory.reset_state_store()


def test_duplicate_escalation_without_lease_is_real(db):
    """把"为什么要租约"钉在代码事实上：台账去重**挡不住重复通知**。

    这是对竞态的**模拟**（不是真并发）：多副本下 B 可能在 A 写回
    `status=escalated` **之前**就读到了同一批到期行。这里显式把 L1 改回 pending
    来复现"B 拿的是过期快照"，然后让 B 也扫一遍——于是**同一个
    `delivery_id`（=hash(alert_x, 2)）被投递两次**，而台账里 L2 仍然只有一行。
    换句话说：`delivery_id` 保证的是"台账不重复"，不是"值班人不被重复打扰"。
    """
    reg = _CountingRegistry()
    eng = _setup_two_levels(reg)
    assert make_delivery_id("alert_x", 2) == make_delivery_id("alert_x", 2)

    eng.sweep_once()                                   # 副本 A 扫（L1 → L2，通知 bob）
    assert len(reg.sent) == 2

    # 模拟"副本 B 的读到的是 A 写回之前的快照"：把 L1 拨回 pending 且仍然超时
    from data_dog.storage import models as M
    from data_dog.storage.db import session_scope
    with session_scope(tenant_id="tenant-a") as s:
        row = s.query(M.AlertDeliveryRow).filter(
            M.AlertDeliveryRow.tenant_id == "tenant-a",
            M.AlertDeliveryRow.level == 1).one()
        row.status = "pending"
        row.ack_deadline = time.time() - 5

    EscalationEngine(registry=reg).sweep_once()        # 副本 B 也扫（无租约保护）
    assert len(reg.sent) == 3, "无租约时同一级通知会真的发第二次——这正是要有租约的原因"
    assert reg.sent[1] == reg.sent[2] == ("webhook", "bob"), \
        f"重复的是**同一条**通知（同一个值班人）：{reg.sent}"
    assert sorted(_levels("tenant-a")) == [(1, "escalated"), (2, "pending")], \
        "台账按 delivery_id 去重：L2 只有一行（且没被 L3 升级，因为没有三级值班）"
