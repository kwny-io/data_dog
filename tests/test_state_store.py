"""共享状态后端契约测试（同一断言跑多后端）。

为什么这样组织
--------------
`StateStore` 是可插拔后端（memory / postgres / redis），必须保证它们**行为一致**
——否则"切后端是配置动作"就是空话。故把断言写成参数化：每个实现跑同一套。

- memory：永远跑（离线零依赖）。
- postgres：设 `DD_TEST_PG_URL` 才跑（与其它 PG 测试同一开关）。
- redis：装 `fakeredis` 才跑（内存版 Redis，离线可测）；否则 skip。
"""
from __future__ import annotations

import os
from typing import Any

import pytest

from data_dog.data_plane.candidate_shared import SharedCandidateBuilder
from data_dog.state.base import StateStore
from data_dog.state.memory import InMemoryStateStore

# ---------------------------------------------------------------- 后端工厂

def _memory() -> StateStore:
    return InMemoryStateStore()


def _pg() -> StateStore:
    from data_dog.state.pg import PostgresStateStore
    return PostgresStateStore(os.environ["DD_TEST_PG_URL"])


def _redis() -> StateStore:
    import fakeredis  # 测试依赖，缺失即 skip

    from data_dog.state.redis import RedisStateStore
    return RedisStateStore(client=fakeredis.FakeRedis(decode_responses=True))


def _backends() -> list[Any]:
    """(id, factory) 列表，按环境可用性筛选。"""
    out: list[Any] = [pytest.param(_memory, id="memory")]
    if os.environ.get("DD_TEST_PG_URL", "").strip():
        out.append(pytest.param(_pg, id="postgres",
                                marks=pytest.mark.postgres))
    try:
        import fakeredis  # noqa: F401
        out.append(pytest.param(_redis, id="redis"))
    except ImportError:
        pass
    return out


# ---------------------------------------------------------------- 契约

@pytest.fixture(params=_backends())
def store(request) -> Any:
    s: StateStore = request.param()
    try:
        # PG 后端需要 state_kv 表；memory/redis 无需建表。
        from data_dog.state.pg import PostgresStateStore
        if isinstance(s, PostgresStateStore):
            from data_dog.storage import migrations
            migrations.upgrade_head(os.environ["DD_TEST_PG_URL"])
            # PG 的 state_kv 是**有意持久**的（共享后端本就跨进程存活），
            # 但测试假定干净起点：不清表则上一轮的 counter/ctr 残留 → 断言全错。
            # memory/redis 每次新建即空，无需此步。
            from sqlalchemy import create_engine, text
            eng = create_engine(os.environ["DD_TEST_PG_URL"], future=True)
            with eng.begin() as c:
                c.execute(text("TRUNCATE state_kv"))
            eng.dispose()
        yield s
    finally:
        s.close()


def test_mutate_read_modify_write(store):
    """基本 RMW：累加器从 None 起算，多次 mutate 累积。"""
    def inc(cur: Any) -> tuple[Any, Any]:
        n = (cur or 0) + 1
        return n, n

    assert store.mutate("counter", inc) == 1
    assert store.mutate("counter", inc) == 2
    assert store.get("counter") == 2


def test_mutate_returns_fn_result(store):
    """mutate 返回的是 fn 的 result，而非写入的值。"""
    def fn(cur: Any) -> tuple[Any, Any]:
        return {"stored": True}, "the-result"

    assert store.mutate("k", fn) == "the-result"
    assert store.get("k") == {"stored": True}


def test_get_missing_is_none(store):
    assert store.get("nope") is None


def test_delete(store):
    store.mutate("k", lambda c: ({"x": 1}, None))
    store.delete("k")
    assert store.get("k") is None


def test_rate_limit_bucket_enforced(store):
    """令牌桶：burst 内放行，超限拒绝，补令牌后恢复。"""
    kw = {"rate_per_sec": 1.0, "burst": 2}
    assert store.rate_limit_allow("dev", now=100.0, **kw) is True
    assert store.rate_limit_allow("dev", now=100.0, **kw) is True
    assert store.rate_limit_allow("dev", now=100.0, **kw) is False   # 桶空
    assert store.rate_limit_allow("dev", now=101.0, **kw) is True    # 补 1 令牌
    assert store.rate_limit_allow("dev", now=101.0, **kw) is False


def test_rate_limit_per_key_isolated(store):
    kw = {"rate_per_sec": 0.0, "burst": 1}
    assert store.rate_limit_allow("a", now=0.0, **kw) is True
    assert store.rate_limit_allow("a", now=0.0, **kw) is False
    assert store.rate_limit_allow("b", now=0.0, **kw) is True        # b 独立


def test_concurrent_mutate_no_lost_updates(store):
    """并发 mutate 同一 key 不丢更新（原子性）。

    memory 后端用锁、PG 用行锁、Redis 用 WATCH——都须保证 N 个并发自增后
    恰好是 N，否则"跨副本共享"就是假的。
    """
    import threading

    n = 24

    def worker() -> None:
        store.mutate("ctr", lambda c: (((c or 0) + 1), None))

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.get("ctr") == n, "并发自增丢更新 → 后端无原子性"


# ---------------------------------------------------------------- 共享候选构建器（跨副本归并）

def test_shared_builder_merges_across_replicas(store):
    """两个"副本"（各持 builder，共享一个 store）的命中归并成同一条候选。

    这是水平扩展的核心断言：若状态在进程内，两条命中会变两条候选。
    """
    a = SharedCandidateBuilder(state_store=store)
    b = SharedCandidateBuilder(state_store=store)
    hit = [{"rule_id": "MOTOR_TEMP_PROTECT", "field": "temperature", "value": 95,
            "severity_hint": "critical", "operator_id": "d@1"}]
    a.add(device_sn="dev", component_ref="joint_motor.hip", metrics={}, hits=hit,
          occurred_at="2026-09-29T10:00:00Z", received_at="x")
    b.add(device_sn="dev", component_ref="joint_motor.hip", metrics={}, hits=hit,
          occurred_at="2026-09-29T10:00:02Z", received_at="x")     # 同窗口内
    cands = a.build()
    assert len(cands) == 1, "跨副本未归并（状态还在进程内）"
    assert cands[0].sample_count == 2


def test_shared_builder_new_group_outside_window(store):
    """超出合并窗口 → 开新的一段候选（与内存版语义一致）。"""
    b = SharedCandidateBuilder(state_store=store, merge_window_s=5.0)
    hit = [{"rule_id": "MOTOR_TEMP_PROTECT", "field": "temperature", "value": 95,
            "severity_hint": "critical", "operator_id": "d@1"}]
    b.add(device_sn="dev", component_ref="joint_motor.hip", metrics={}, hits=hit,
          occurred_at="2026-09-29T10:00:00Z", received_at="x")
    b.add(device_sn="dev", component_ref="joint_motor.hip", metrics={}, hits=hit,
          occurred_at="2026-09-29T10:01:00Z", received_at="x")     # 60s 后 → 超窗
    assert len(b.build()) == 2


# ---------------------------------------------------------------- 后端选择

def test_rate_backend_follows_state_backend_by_default(monkeypatch):
    """未设 DD_RATE_BACKEND → 限流后端与状态后端**同源**（同一实例）。"""
    from data_dog.state import factory
    monkeypatch.delenv("DD_RATE_BACKEND", raising=False)
    monkeypatch.setenv("DD_STATE_BACKEND", "memory")
    factory.reset_state_store()
    try:
        assert factory.get_rate_store() is factory.get_state_store()
    finally:
        factory.reset_state_store()


def test_rate_backend_can_be_split(monkeypatch):
    """设 DD_RATE_BACKEND → 限流后端与状态后端**分离**（不同实例）。

    这是"限流走 Redis、业务状态走 PG"的落点：调用点不感知，只认接口。
    """
    from data_dog.state import factory
    monkeypatch.setenv("DD_STATE_BACKEND", "memory")
    monkeypatch.setenv("DD_RATE_BACKEND", "memory")     # 同为 memory 但**独立**实例
    factory.reset_state_store()
    try:
        state, rate = factory.get_state_store(), factory.get_rate_store()
        assert state is not rate, "拆分配置下两者不应是同一实例"
        # 分离后各自独立：限流桶的 key 不污染业务状态
        rate.rate_limit_allow("d", rate_per_sec=0, burst=1, now=0)
        assert state.get("rate:d") is None
    finally:
        factory.reset_state_store()
