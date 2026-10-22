"""多副本限流一致性：**两个真服务进程** + 同一个真 PostgreSQL，经 HTTP 打。

为什么要这条（它补的是前面三条用例的盲区）
------------------------------------------
- `tests/test_state_store.py`：证明存储层原子（`FOR UPDATE`，单位是"一次调用"）。
- `tests/test_state_multiprocess.py`：证明**进程之间**共享同一份 store。
- 但这两条都不经过**服务边界**：网关实例化、`now` 从哪儿来、依赖注入、鉴权都在那条路上。
  `#101` 就是这么漏掉的——存储层用例全绿，而生产入口 `TelemetryGateway` 自己在往共享桶里
  写 `time.monotonic()`。单元测试救不了它，因为单元测试**显式传 `now=`**。

所以这里不模拟：真起两个 `python -m data_dog.service`（不同端口、同一 PG），
把同一设备的流量交替打到两个副本，断言**总放行量 = burst**（而不是每个副本各自放开 burst）。

判据为什么是"总数恰好 5"而不是"两个副本都放过/都被拒"：
令牌桶是跨副本共享的，`DD_RATE_PER_SEC=0`（不补令牌）+ `DD_RATE_BURST=5` → 全局只有 5 个令牌，
无论它们被谁花掉。若状态落回进程内（或时钟不可比），总数会变成 10，或其中一个副本直接全拒。

⚠ 观测字段的坑（第一次写这条用例时就踩了）：`stats.ingested` 在 `process_one` 的**入口**就
`+= 1`，被拒的事件**也算进去**（`data_plane/pipeline.py:84-90`）。所以"真正落库了多少"要看
`stats.normalized`（或 `persisted.telemetry`），不能用 `ingested`。这也是 #102 的根：
`200 {"ok": true}` + `ingested=100` 配上 `rejected=100, normalized=0` 看上去毫无异常。

诚实边界
--------
- 只测**限流**这一条共享语义最直接可观测的路径；候选合并跨副本归并由
  `test_state_store.py::test_shared_builder_merges_across_replicas` 覆盖。
- 需要 `DD_TEST_PG_URL`（`pytest -m postgres`），会**临时建一个独立库**并在结束后删掉，
  不碰 `DD_TEST_PG_URL` 指向的那个库，因此可以和其它 PG 用例共存。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

pytestmark = pytest.mark.postgres

_PG_URL = os.environ.get("DD_TEST_PG_URL", "")
_API_KEY = "multireplica-test-key"
_BURST = 5
_READY_TIMEOUT_S = 120.0
_REQUESTS = 10          # 交替打到两个副本，共 10 次；burst=5 → 只该过 5 次


def _event(idx: int) -> dict[str, Any]:
    """一条能通过网关校验的原始日志（字段集合与 `tests/test_data_plane.py::_raw` 一致）。"""
    return {
        "event_id": f"mr-{idx}",
        "occurred_at": "2026-09-13T10:00:00Z",
        "received_at": "2026-09-13T10:00:00.1Z",
        "sequence": idx,
        "vendor": "unitree",
        "model": "go2",
        "firmware": "3.2.1",
        "protocol_version": "motor-log-v2",
        "component_hint": "joint_motor.front_left_hip",
    }


# ---------------------------------------------------------------- 基础设施

def _admin_conn():
    """连到同一台 PG 的 `postgres` 库（用来建/删本用例独占的库）。"""
    import psycopg
    from sqlalchemy.engine.url import make_url

    url = make_url(_PG_URL)
    return psycopg.connect(
        host=url.host, port=url.port or 5432, user=url.username,
        password=url.password, dbname="postgres", autocommit=True,
    )


def _with_db(name: str, url: str) -> str:
    """把连接串里的库名换掉（保留驱动前缀，便于 SQLAlchemy 直接用）。"""
    head, _, _ = url.rpartition("/")
    return f"{head}/{name}"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(port: int, *, timeout: float = _READY_TIMEOUT_S) -> dict[str, Any]:
    """等 `/readyz` 返回 200（它就绪即代表迁移已到 head **且**表都在，见 #97）。"""
    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/readyz", timeout=2.0) as r:
                if r.status == 200:
                    return json.loads(r.read().decode("utf-8"))
        except Exception as e:                       # noqa: BLE001
            last = e
        time.sleep(0.25)
    raise AssertionError(f"服务未在 {timeout}s 内就绪（最后错误：{last!r}）")


def _post_telemetry(port: int, *, tenant: str, device: str,
                    idx: int) -> dict[str, Any]:
    body = json.dumps({"events": [_event(idx)]}).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/telemetry", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": _API_KEY,
                 "X-Tenant-Id": tenant, "X-Device-Sn": device},
    )
    with urllib.request.urlopen(req, timeout=30.0) as r:
        return json.loads(r.read().decode("utf-8"))


def _spawn(port: int, dsn: str, log_path: str) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "DD_SERVICE_PORT": str(port),
        "DD_API_KEY": _API_KEY,
        "DD_DATABASE_URL": dsn,
        "DD_AUTO_MIGRATE": "1",          # 幂等：迁移已在 head 时是 no-op
        "DD_STATE_BACKEND": "postgres",  # 多副本硬要求：状态必须外置
        "DD_RATE_BACKEND": "",           # 空 = 跟随状态后端（不拆）
        "DD_RATE_PER_SEC": "0",          # 不补令牌 → 只剩 burst 个
        "DD_RATE_BURST": str(_BURST),
        "DD_LLM_VENDOR": "fake",         # 本用例不碰 LLM
    })
    # 文件句柄故意不关：它随子进程一起活到用例结束（关掉会让服务进程丢 stdout）
    log = open(log_path, "wb")           # noqa: SIM115
    return subprocess.Popen([sys.executable, "-m", "data_dog.service"],
                            env=env, stdout=log, stderr=subprocess.STDOUT)


# ---------------------------------------------------------------- 用例

@pytest.fixture(scope="module")
def two_replicas(tmp_path_factory):
    """起两个副本（同一 PG），yield (ports, dsn)。"""
    if not _PG_URL:
        pytest.skip("需要 DD_TEST_PG_URL（真 PostgreSQL）")

    from data_dog.storage.migrations import upgrade_head

    db = f"dd_multireplica_{os.getpid()}_{int(time.time()) % 100000}"
    dsn = _with_db(db, _PG_URL)
    admin = _admin_conn()
    admin.execute(f'CREATE DATABASE "{db}"')
    try:
        upgrade_head(dsn)                      # 建表一次；两个副本共享
        procs: list[subprocess.Popen] = []
        ports = [_free_port(), _free_port()]
        logdir = tmp_path_factory.mktemp("replicas")
        try:
            for i, port in enumerate(ports):
                procs.append(_spawn(port, dsn, str(logdir / f"replica{i}.log")))
            for port in ports:
                _wait_ready(port)
            yield ports, dsn
        finally:
            for p in procs:
                p.terminate()
            for p in procs:
                try:
                    p.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    p.kill()
    finally:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (db,))
        admin.execute(f'DROP DATABASE IF EXISTS "{db}"')
        admin.close()


def test_两个副本必须共用一个令牌桶(two_replicas):
    """半量打到 A、半量打到 B，总放行量必须是 burst，而不是每副本各 burst。

    这条断言在"状态在进程内"或"时钟不可比"两种实现下都会变红：
    - 进程内桶：每个副本各自放行 5 次 → 总数 10；
    - 时钟不可比（#101）：后写的副本算出的 elapsed≤0 → 令牌只扣不补 →
      另一个副本从此全拒（总数偏小，且被拒的那一侧永远恢复不了）。
    """
    ports, _dsn = two_replicas
    tenant, device = "tenant-mr", "device-mr"
    landed = 0
    per_port = {ports[0]: 0, ports[1]: 0}
    for idx in range(_REQUESTS):
        port = ports[idx % 2]                     # 严格交替：5 次 A、5 次 B
        body = _post_telemetry(port, tenant=tenant, device=device, idx=idx)
        # 用 normalized（真落库）而不是 ingested（含被拒，见模块 docstring 的坑）
        got = int(body.get("stats", {}).get("normalized", 0))
        landed += got
        per_port[port] += got

    assert landed == _BURST, (
        f"跨副本没有共享令牌桶：真落库 {landed} 条（期望 {_BURST}）"
        f"，分布 {per_port}（若两边各 {_BURST} 条 = 桶在进程内）")
    assert sum(per_port.values()) == landed


def test_限流是失败关闭且可观测的(two_replicas):
    """被限流的请求必须留下可观测痕迹（`rejected_by_reason` 里有 rate_limit）。"""
    ports, _dsn = two_replicas
    tenant, device = "tenant-mr", "device-mr"      # 与上一条同一 key，桶已空
    body = _post_telemetry(ports[1], tenant=tenant, device=device, idx=999)
    stats = body.get("stats", {})
    assert int(stats.get("normalized", 0)) == 0, "桶已空却仍落库（限流没生效）"
    reasons = stats.get("rejected_by_reason") or {}
    assert any("rate" in str(k).lower() for k in reasons), (
        f"被限流但没在 rejected_by_reason 里体现：{reasons!r}")
    # 顺带钉住 #102 的现状：HTTP 仍是 200 + ok=true，"全部被拒"只能从 stats 看出来
    assert body.get("ok") is True


def test_鉴权仍然失败关闭(two_replicas):
    """共享状态改造不得顺手放松鉴权：无密钥 → 401。"""
    ports, _dsn = two_replicas
    req = urllib.request.Request(
        f"http://127.0.0.1:{ports[0]}/v1/telemetry",
        data=json.dumps({"events": [_event(1)]}).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "X-Tenant-Id": "t", "X-Device-Sn": "d"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10.0)
    assert exc.value.code == 401
