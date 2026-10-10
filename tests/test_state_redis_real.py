"""Redis 后端的**真服务**契约测试（设 `DD_TEST_REDIS_URL` 才跑，否则跳过）。

为什么还要这份（不是重复 `test_state_store.py`）
----------------------------------------------
`tests/test_state_store.py` 的 `[redis]` 参数用的是 **fakeredis**（内存实现），
它证明的是"我们的重试 / 本地锁逻辑自洽"，**证明不了**"真 Redis 的
WATCH/MULTI/EXEC 语义与我们假设一致"——例如 EXEC 在什么时机返回 None、
`pipeline.reset()` 是否真的解掉 WATCH、并发下 `redis.exceptions.WatchError`
是否按预期抛出。CI 用 service container 起一个真 `redis:7` 跑这份测试，
把"仿真实测"升级成"真服务实测"。

本地没有 Redis 时自动跳过——与 PG 集成测试（`DD_TEST_PG_URL`）同一套开关思路，
也保证默认套件仍然全离线。
"""
from __future__ import annotations

import os
import threading

import pytest

from data_dog.state.redis import RedisStateStore

URL = os.environ.get("DD_TEST_REDIS_URL", "").strip()

pytestmark = [
    pytest.mark.redis,
    pytest.mark.skipif(not URL, reason="未设 DD_TEST_REDIS_URL，跳过真 Redis 集成测试"),
]


@pytest.fixture()
def store():
    from data_dog.state.redis import make_client

    # 用生产同一处构造口径（含 RESP2 显式声明），否则这份"真服务契约测试"验的
    # 就不是线上那条连接路径，Redis 5.x 上的 HELLO 不兼容反而测不出来。
    client = make_client(URL)
    client.flushdb()
    s = RedisStateStore(client=client)
    try:
        yield s
    finally:
        client.flushdb()
        client.close()


def test_mutate_roundtrip_and_empty_semantics(store):
    """真服务上的基本往返：写入可见、空占位 `{}` 归一为 None（与其它后端一致）。"""
    store.mutate("k", lambda cur: ({"x": 1}, None))
    assert store.get("k") == {"x": 1}
    store.mutate("empty", lambda cur: ({}, None))
    assert store.get("empty") is None
    print("PASS test_mutate_roundtrip_and_empty_semantics")


def test_concurrent_mutate_on_real_redis_no_lost_updates(store):
    """24 线程并发自增同一 key → 恰好 24（真 Redis 上不许丢更新，也不许抛错）。"""
    n = 24
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            store.mutate("ctr", lambda c: (((c or 0) + 1), None))
        except BaseException as e:          # noqa: BLE001 - 失败要显式记录，不能吞
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"并发 mutate 抛错：{errors[:3]}"
    assert store.get("ctr") == n, "真 Redis 上出现丢更新"
    print("PASS test_concurrent_mutate_on_real_redis_no_lost_updates")


def test_rate_limit_bucket_on_real_redis(store):
    """限流桶在真服务上按 burst 放行、超限拒绝（热路径的真实语义）。"""
    kw = {"rate_per_sec": 0.0, "burst": 3}
    assert [store.rate_limit_allow("dev", now=1.0, **kw) for _ in range(4)] == \
        [True, True, True, False]
    print("PASS test_rate_limit_bucket_on_real_redis")


def test_external_writer_conflict_is_retried(store):
    """另一个客户端在 WATCH 之后改同一 key → 我们重试并最终成功（乐观锁语义）。

    这条只能在**真服务**上验：fakeredis 的 pipeline 语义不保证同样时机。
    """
    from data_dog.state.redis import make_client

    other = make_client(URL)
    try:
        store.mutate("race", lambda cur: ({"v": 1}, None))
        seen: list[object] = []

        def fn(cur):
            seen.append(cur)
            if len(seen) == 1:              # 第一次进来时，用另一个连接抢先写
                other.set("race", '{"v": 999}')
            return ({"v": (cur or {}).get("v", 0) + 1}, None)

        store.mutate("race", fn)
        assert len(seen) >= 2, "外部写入应导致重试（fn 被重跑）"
        # 重试必须基于**外部写入之后的新值**重算：999 + 1 = 1000。
        # 若这里得到 2，说明拿的是旧快照 → 乐观锁语义是假的。
        assert store.get("race") == {"v": 1000}, store.get("race")
    finally:
        other.close()
    print("PASS test_external_writer_conflict_is_retried")
