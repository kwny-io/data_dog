"""摄入数据面必须**跨请求**存活，且返回的 `stats` 是**本批增量**。

这两条是同一次事故的两面，2026-10-31 一起修掉：

1. `POST /v1/telemetry` 原先每个请求都新建 DataPlane。`WindowBuffer` 是有状态的，
   窗口永远从空开始，一批约 10 条事件（10Hz / 每 200ms）只有 1 秒跨度，而
   `MOTOR_STALL_SUSTAINED` 的前置是 `window.span() >= 5.0`——**规则恒假**。
   症状：同一次堵转，仿真器进程内报得出来，推到后端一条都没有。

2. 数据面改成长驻之后，`self.stats` 变成进程累计。若原样返回，调用方会拿到
   "整个进程至今"的数字：接口回执里的 `rejected` 越积越大，而
   `metrics.observe_data_plane()` 是 `.inc(...)`，传累计值等于按请求数平方加。
   所以 `process_batch` 返回差值。

为什么这两个必须有测试：两条都不会让任何"看起来正常"的检查变红。
前者只表现为该报的故障不报；后者只表现为数字慢慢变得离谱。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from data_dog.storage.db import init_db, reset_engine

_AUTH = {"X-Tenant-Id": "tenant-real", "X-Device-Sn": "device-go2-0001"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'plane.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    reset_engine()
    init_db(url)
    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        yield c
    reset_engine()


def _raw(event_id: str, temperature: float = 95.0,
         firmware: str = "3.2.1") -> dict:
    return dict(
        event_id=event_id, occurred_at="2026-09-13T10:00:00Z",
        received_at="2026-09-13T10:00:00.100Z", sequence=1,
        vendor="unitree", model="go2", firmware=firmware,
        protocol_version="motor-log-v2",
        component_hint="joint_motor.front_left_hip",
        payload_inline=json.dumps(
            {"temperature": temperature, "current": 12.0, "mode": 0}),
    )


def test_plane_survives_request_boundary(client):
    """跨请求的窗口必须累积：两次请求之后窗口里应有两条事件的样本。

    判据用 `normalized`（本批成功规范化的事件数）不足以证明，所以直接问
    数据面对象本身——它就是请求之间应当被复用的那个东西。
    """
    from data_dog.service import runtime

    r1 = client.post("/v1/telemetry", headers=_AUTH,
                     json={"events": [_raw("a")]})
    assert r1.status_code == 200
    plane_after_first, _lock = runtime.get_ingest_plane()

    r2 = client.post("/v1/telemetry", headers=_AUTH,
                     json={"events": [_raw("b")]})
    assert r2.status_code == 200
    plane_after_second, _lock2 = runtime.get_ingest_plane()

    assert plane_after_first is plane_after_second, (
        "接入请求之间数据面被换掉了——WindowBuffer 的窗口不会跨请求累积，"
        "依赖 span() 门槛的持续性规则（如 MOTOR_STALL_SUSTAINED）将恒假"
    )
    key = "device-go2-0001|joint_motor.front_left_hip"
    n = plane_after_second.window.sample_count(key)
    assert n >= 2, (
        f"窗口里 key={key!r} 只有 {n} 个样本；两次请求各一条事件，至少应有 2 条。"
        "窗口没有跨请求累积。"
    )


def test_stats_are_per_batch_not_cumulative(client):
    """回执里的 `stats` 必须只描述**这一批**。

    长驻数据面下 `self.stats` 是进程累计；原样返回会让每个请求都报
    "整个进程至今拒了多少"，而同一个数字还会被 `metrics.observe_data_plane`
    以 `.inc()` 再加一遍。
    """
    # 第一批：正常事件
    a = client.post("/v1/telemetry", headers=_AUTH,
                    json={"events": [_raw("s1")]}).json()
    assert a["stats"]["ingested"] == 1

    # 第二批：固件不在兼容范围 → 本条被拒。本批 ingested 必须是 1，不是 2。
    b = client.post("/v1/telemetry", headers=_AUTH,
                    json={"events": [_raw("s2", firmware="9.9.9")]}).json()
    assert b["stats"]["ingested"] == 1, (
        f"第二批 ingested={b['stats']['ingested']}，说明返回的是累计值"
    )
    assert b["stats"]["rejected"] == 1

    # 第三批：又一条正常事件。前两批的 reject 不该再出现。
    c = client.post("/v1/telemetry", headers=_AUTH,
                    json={"events": [_raw("s3")]}).json()
    assert c["stats"]["ingested"] == 1
    assert c["stats"]["rejected"] == 0, (
        f"第三批 rejected={c['stats']['rejected']}，说明上一批的拒绝被带过来了"
    )
    assert c["stats"]["rejected_by_reason"] == {}, (
        f"第三批 rejected_by_reason={c['stats']['rejected_by_reason']}，应为空"
    )
