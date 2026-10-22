"""跨**进程**的共享状态一致性：真开多个进程打同一个 PG，而不是在一个进程里假装多副本。

为什么需要这个文件（已有测试不够）
----------------------------------
`tests/test_state_store.py` 的并发断言是**线程**，`tests/test_lease.py` 更是
"全离线：memory 状态后端 + 假时钟"。它们能证明"同一进程内多线程看到的语义一致"，
却证明不了**跨进程**的真实约束——而生产的多副本是两个独立进程、通常是两台机器：

- **事务/行锁**是不是真跨进程生效（同进程线程可能被同一把 Python 锁或同一个连接串起来，
  于是"原子"成了假象）；
- **限流桶**是不是真共享（若桶落在进程内变量里，线程测试照样绿，生产却是"每个副本
  各挡各的"，实际配额翻 N 倍）；
- **租约选主**是不是只有一个赢家（线程版可以靠 GIL 蒙对）。

所以这里用 `multiprocessing`（Windows 走 spawn，与"独立进程"更接近）真起进程，
用共同的 `start_at` 墙上时刻对齐起跑。不用 `Barrier`：spawn 下传同步原语容易踩句柄
继承的坑，而一次自旋对齐对"同时抢"已经足够。

只在设了 `DD_TEST_PG_URL` 时跑（`-m postgres`，与其它 PG 用例同一开关）。
memory 后端**本来就不提供**跨进程保证（各进程各一份状态），拿它测这个没有意义——
那正是本文件要防的那个误解。

变异矩阵（本文件真的会红吗？——手改源码验证过，不是声称）
--------------------------------------------------------
| 变异 | `test_lease_has_exactly_one_winner` | `test_rate_limit_quota_is_shared` | `test_mutate_has_no_lost_updates` |
|---|---|---|---|
| 去掉 `state/pg.py` 两处 `FOR UPDATE`（行锁消失） | ✅ 绿（**不敏感**） | ❌ 红：实际放行 12 次（应 5） | ❌ 红：33 ≠ 100 |
| 把桶 key 改成按 pid 分片（= 每副本各挡各的） | ✅ 绿 | ❌ 红：实际放行 20 次（应 5） | ✅ 绿 |

诚实说明：**租约用例对原子性变异不敏感**——6 个进程各抢一次，丢原子性的窗口太小，
可能靠运气蒙对。原子性这件事由 `test_mutate_has_no_lost_updates` 负责兜（它一变异就
33≠100）；租约用例守的是**语义不变量**（赢家唯一、共享状态记录一致）。要检测
原子性回归，看计数器那条，不要看租约那条。
"""
from __future__ import annotations

import multiprocessing as mp
import os
import time
from typing import Any

import pytest

pytestmark = pytest.mark.postgres

_URL_ENV = "DD_TEST_PG_URL"
_LEAD_S = 1.5
_JOIN_S = 180


def _url() -> str:
    return os.environ.get(_URL_ENV, "").strip()


def _pg_store() -> Any:
    from data_dog.state.pg import PostgresStateStore
    return PostgresStateStore(_url())


def _wait_until(start_at: float) -> None:
    """自旋到约定时刻，让各进程尽量同时冲进临界区。"""
    while time.time() < start_at:
        pass


# ------------------------------------------------------------- 子进程入口
# 必须是模块级函数：spawn 下靠 pickle 按"模块名 + 函数名"找回。

def _lease_worker(payload: tuple[str, str, str, float, Any]) -> None:
    """抢一次租约，把 (owner, 是否抢到) 丢回队列。"""
    key, owner, start_at, q = payload  # 注意：payload 里不含 url，从环境读
    store = _pg_store()
    try:
        from data_dog.state.lease import Lease
        _wait_until(start_at)
        won = Lease(store, key, owner, ttl_s=30.0).acquire()
        q.put(("lease", owner, won))
    finally:
        store.close()


def _rate_worker(payload: tuple[str, int, float, str, Any]) -> None:
    """从共享桶里连抢 `attempts` 次，把放行结果丢回队列。"""
    key, attempts, start_at, replica_id, q = payload
    store = _pg_store()
    try:
        _wait_until(start_at)
        # rate_per_sec=0：不补令牌 → "共放行几次"完全由共享桶决定，与时钟无关。
        oks = [store.rate_limit_allow(key, rate_per_sec=0.0, burst=5)
               for _ in range(attempts)]
        q.put(("rate", replica_id, oks))
    finally:
        store.close()


def _increment_worker(payload: tuple[str, int, float]) -> None:
    """对同一 key 自增 `n` 次。"""
    key, n, start_at = payload
    store = _pg_store()
    try:
        _wait_until(start_at)

        def inc(cur: Any) -> tuple[Any, Any]:
            return (cur or 0) + 1, None

        for _ in range(n):
            store.mutate(key, inc)
    finally:
        store.close()


def _spawn_and_join(target: Any, payloads: list[tuple]) -> None:
    """起 len(payloads) 个进程并等全部结束；任一非 0 退出即失败。"""
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=target, args=(p,)) for p in payloads]
    for p in procs:
        p.start()
    try:
        for i, p in enumerate(procs):
            p.join(timeout=_JOIN_S)
            assert not p.is_alive(), f"第 {i} 个子进程超时未结束"
            assert p.exitcode == 0, f"第 {i} 个子进程失败 exitcode={p.exitcode}"
    finally:
        for p in procs:
            if p.is_alive():                      # pragma: no cover - 仅在超时时发生
                p.terminate()


@pytest.fixture()
def clean_state() -> None:
    """干净起点：`state_kv` 是**有意持久**的（共享后端本就跨进程存活），
    上一轮残留的 key 会让断言变成"上次跑剩什么"。"""
    if not _url():
        pytest.skip(f"未设 {_URL_ENV}（跨进程一致性测试需要真 PG）")
    from sqlalchemy import create_engine, text

    from data_dog.storage import migrations
    migrations.upgrade_head(_url())
    eng = create_engine(_url(), future=True)
    try:
        with eng.begin() as c:
            c.execute(text("TRUNCATE state_kv"))
    finally:
        eng.dispose()


# ------------------------------------------------------------------ 用例

def test_lease_has_exactly_one_winner_across_processes(clean_state):
    """6 个进程同时抢同一把租约 → **有且只有一个**赢家，且共享状态里记的就是它。

    这是"file 源只被一个副本读""告警 sweeper 只被一个副本扫"的前提。
    若 `mutate` 的原子性在跨进程下不成立，这里会出现多个 True。
    """
    from data_dog.state.lease import Lease

    key = "leases:multiproc"
    owners = [f"replica-{i}" for i in range(6)]
    ctx = mp.get_context("spawn")
    q: Any = ctx.Queue()
    start_at = time.time() + _LEAD_S

    _spawn_and_join(_lease_worker, [(key, o, start_at, q) for o in owners])

    results = [q.get(timeout=30) for _ in owners]
    winners = [owner for kind, owner, won in results if kind == "lease" and won]
    assert len(winners) == 1, f"租约赢家必须唯一，实际 {winners}"

    store = _pg_store()
    try:
        assert Lease(store, key, "probe", ttl_s=30.0).holder() == winners[0], \
            "共享状态里的持有者应与赢家一致"
    finally:
        store.close()


def test_rate_limit_quota_is_shared_across_processes(clean_state):
    """4 个进程各抢 5 次、burst=5 → **总共只放行 5 次**。

    若桶落在进程内（或按进程分片），总放行会是 20：限流形同虚设，而单进程线程
    测试照样绿。这就是"配额翻 N 倍"的事故形态。
    """
    key = "multiproc-rate"
    per_replica, replicas, burst = 5, 4, 5
    ctx = mp.get_context("spawn")
    q: Any = ctx.Queue()
    start_at = time.time() + _LEAD_S

    _spawn_and_join(_rate_worker, [(key, per_replica, start_at, f"r{i}", q)
                                   for i in range(replicas)])

    allowed = 0
    seen: list[str] = []
    for _ in range(replicas):
        kind, rid, oks = q.get(timeout=30)
        assert kind == "rate", f"意外的消息：{kind}"
        assert len(oks) == per_replica, f"{rid} 的尝试次数不对：{len(oks)}"
        seen.append(rid)
        allowed += sum(1 for ok in oks if ok)

    assert sorted(seen) == [f"r{i}" for i in range(replicas)], f"副本结果不全：{seen}"
    assert allowed == burst, (
        f"burst={burst}，4 个副本合计只应放行 {burst} 次，实际 {allowed} 次"
        f"（{replicas} × {per_replica} 次尝试）"
    )


def test_mutate_has_no_lost_updates_across_processes(clean_state):
    """4 进程各自增 25 次 → 恰好 100。丢更新在这里会立刻显形。"""
    key = "multiproc-counter"
    per, replicas = 25, 4
    _spawn_and_join(_increment_worker, [(key, per, time.time() + _LEAD_S)
                                        for _ in range(replicas)])

    store = _pg_store()
    try:
        assert store.get(key) == per * replicas
    finally:
        store.close()
