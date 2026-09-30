"""测试级全局夹具：隔离**进程级单例**与**遗留后台线程**，防跨测试污染。

为什么需要
----------
被测代码里有两类进程级全局单例：
1. `storage.db._engine` / `_SessionLocal`——缓存的引擎（`reset_engine()` 可清）。
2. `state.factory._state` / `_rate`——共享状态后端（`reset_state_store()` 可清）。

**真实踩到的竞态**：异步调查的 worker 是 `ThreadPoolExecutor` 的后台线程，client
fixture 关停时用 `stop(wait=False)`——**不等线程退出**。于是测试结束后常有
`dd-investigate_N` 线程仍在跑循环：等 `monkeypatch` 回滚了 `DD_DATABASE_URL`、
下一个测试又调了 `reset_engine()` 时，它恰好在空窗期调 `get_engine()`，读到**空
env → 默认库** `data_dog.db`，把这个错误引擎写进全局单例；下个测试 `session_scope()`
连到该空库，报随机的 `no such table: investigation_job`（单独跑必过、叠加跑 ~2/3 失败）。

因此 teardown 必须**先等遗留 worker 线程真正结束**，再清单例——否则清完又立刻被
残留线程重新污染。清空 StateStore 会清掉查询队列，恒等循环下一轮即空转并响应停止。
"""
from __future__ import annotations

import threading
import time

import pytest

from data_dog.state import reset_state_store
from data_dog.storage.db import reset_engine

_WORKER_PREFIX = "dd-investigate"


def _join_lingering_workers(timeout_s: float = 5.0) -> None:
    """等 admin 后台 worker 线程退出（带总超时，避免死等）。"""
    deadline = time.monotonic() + timeout_s
    for _ in range(100):
        lingering = [t for t in threading.enumerate()
                     if t.name.startswith(_WORKER_PREFIX) and t.is_alive()]
        if not lingering:
            return
        for t in lingering:
            t.join(timeout=max(0.0, deadline - time.monotonic()))


@pytest.fixture(autouse=True)
def _isolate_globals():
    """测试前后各清一次引擎与共享状态后端，并排干遗留 worker 线程。"""
    reset_engine()
    reset_state_store()
    _join_lingering_workers()
    yield
    # 先清 StateStore（清空待办队列→worker 下一轮空转并见 stop）、再等线程退出，
    # 最后清单例——顺序反了会被残留线程重新写成错误引擎。
    reset_state_store()
    _join_lingering_workers()
    reset_engine()
    reset_state_store()
