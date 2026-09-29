"""P4-8 软实时回归：热路径零 LLM + 端到端延迟可观测。

为什么这些断言重要
------------------
P4-8 的两条声明必须由**测试**守住，否则会随重构悄悄失效：
1. **热路径零 LLM**：`POST /v1/telemetry` 只做网关→解析→检测→落库，**绝不**触发
   调查/大模型。若某次重构把调查顺手接进接入路径，延迟会从毫秒级跳到秒级，
   而普通功能测试仍会通过——只有断言"调查计数零增长"才拦得住。
2. **延迟可观测**：热路径有 `http_request_duration_seconds` 直方图，压测能对照
   客户端与服务端两侧数字（`tools/loadgen.py --verify-server`）。

与 `tools/loadgen.py` 的分工：那是**实测数字**工具（人跑）；这里是**声明护栏**
（CI 跑），确保声明不被无声破坏。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from data_dog.core import metrics
from data_dog.storage.db import init_db, reset_engine

_AUTH = {"X-Tenant-Id": "t-realtime", "X-Device-Sn": "dev-rt-1"}


def _raw(event_id="rt1", temperature=95):
    return {
        "event_id": event_id, "occurred_at": "2026-09-13T10:00:00Z",
        "received_at": "2026-09-13T10:00:00.1Z", "sequence": 1,
        "vendor": "unitree", "model": "go2", "firmware": "3.2.1",
        "protocol_version": "motor-log-v2",
        "component_hint": "joint_motor.front_left_hip",
        "payload_inline": json.dumps({"temperature": temperature, "current": 12.0,
                                      "mode": 0}),
    }


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'rt.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    reset_engine()
    init_db(url)
    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        yield c
    reset_engine()


def _counter(family, name: str) -> float:
    """读取一个 Counter 的当前总量（所有 label 系列之和）。"""
    total = 0.0
    for metric in family.collect():
        if metric.name != name:
            continue
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                total += sample.value
    return total


def test_telemetry_hot_path_does_not_investigate(client):
    """热路径零 LLM：接入不触发任何调查（P4-8 验收点 5）。

    接入**大量**过温事件（本会产生 critical 候选）后，调查相关计数必须纹丝不动。
    """
    inv_family = metrics.investigations_total
    job_family = metrics.investigation_jobs_total
    inv_before = _counter(inv_family, "data_dog_investigations_total")
    job_before = _counter(job_family, "data_dog_investigation_jobs_total")

    for i in range(20):
        r = client.post("/v1/telemetry", headers=_AUTH,
                        json={"events": [_raw(f"rt{i}", temperature=96)]})
        assert r.status_code == 200, r.text

    inv_after = _counter(inv_family, "data_dog_investigations_total")
    job_after = _counter(job_family, "data_dog_investigation_jobs_total")
    assert inv_after == inv_before, "热路径触发了调查——违反「零 LLM」"
    assert job_after == job_before, "热路径触发了异步调查——违反「零 LLM」"


def test_telemetry_records_request_duration(client):
    """接入路径必须被耗时直方图观测，压测两侧对照才有依据（P4-8 验收点 2）。"""
    fam = metrics.http_request_duration_seconds

    def telemetry_count() -> float:
        return sum(s.value for m in fam.collect() for s in m.samples
                   if s.name.endswith("_count")
                   and s.labels.get("path") == "/v1/telemetry")

    before = telemetry_count()
    r = client.post("/v1/telemetry", headers=_AUTH, json={"events": [_raw("d1")]})
    assert r.status_code == 200, r.text
    assert telemetry_count() > before, "接入路径未被 http_request_duration_seconds 观测"
