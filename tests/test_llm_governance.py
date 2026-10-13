"""LLM 治理与韧性的回归：额度硬熔断、队列准入、提交幂等、有界重试与降级。

为什么单独一组
--------------
这四件事都属于"出事时才发现没做"的类型：额度超了才看账单、队列爆了才发现没背压、
重复提交了才发现没幂等、上游抖一下才发现没重试。所以每一条都钉**行为**而不只是接口存在：
- 额度：超了必须 429，且这次尝试要**留痕**（跑运行日志里能查到 error_code）；
- 队列：超了必须在**落 job 之前**拒，别制造"假的受理"；
- 幂等：同一 key 只允许真跑一次，第二次拿到同一结果；
- 重试：只重试可重试的错，且**不越过 deadline**（"没时间了"不能被重试放大）。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from data_dog.errors import ToolErrorCode
from data_dog.providers.client import LLMCallError
from data_dog.service import runtime as rt
from data_dog.state import reset_state_store
from data_dog.storage.db import init_db, reset_engine

_AUTH = {"X-Tenant-Id": "tenant-real", "X-Device-Sn": "device-go2-0001"}
_Q = {"X-Tenant-Id": "tenant-real"}


def _raw(event_id="e1", temperature=96, **kw):
    base = dict(
        event_id=event_id, occurred_at="2026-09-13T10:00:00Z",
        received_at="2026-09-13T10:00:00.100Z", sequence=1,
        vendor="unitree", model="go2", firmware="3.2.1",
        protocol_version="motor-log-v2",
        component_hint="joint_motor.front_left_hip",
        payload_inline=json.dumps(
            {"temperature": temperature, "current": 12.0, "mode": 0}),
    )
    base.update(kw)
    return base


def _client(tmp_path, monkeypatch, *, async_mode: bool):
    url = f"sqlite:///{(tmp_path / 'gov.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    monkeypatch.setenv("DD_INVESTIGATE_OFFLINE", "1")
    if async_mode:
        monkeypatch.setenv("DD_INVESTIGATE_ASYNC", "1")
        monkeypatch.setenv("DD_INVESTIGATE_WORKERS", "1")
        monkeypatch.setenv("DD_INVESTIGATE_QUEUE_POLL_MS", "20")
    else:
        monkeypatch.delenv("DD_INVESTIGATE_ASYNC", raising=False)
    reset_engine()
    reset_state_store()
    init_db(url)
    from data_dog.service.app import create_app
    return TestClient(create_app())


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, async_mode=False) as c:
        yield c
    reset_engine()
    reset_state_store()


@pytest.fixture()
def async_client(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, async_mode=True) as c:
        yield c
    reset_engine()
    reset_state_store()


def _seed_candidate(c, event_id="e1"):
    c.post("/v1/telemetry", headers=_AUTH, json={"events": [_raw(event_id)]})
    return c.get("/v1/candidates", headers=_Q).json()["items"][0]["candidate_id"]


# ---------------------------------------------------------------- A8 重试 / 降级

class _Flaky:
    """按次数失败的假 provider：失败 N 次后成功。记录每次拿到的 model。"""

    def __init__(self, failures=1, code=ToolErrorCode.UNAVAILABLE, retryable=None):
        self.failures = failures
        self.code = code
        self.retryable = retryable
        self.calls = 0
        self.models: list[str | None] = []

    def chat(self, messages, **kw):
        self.calls += 1
        self.models.append(kw.get("model"))
        if self.calls <= self.failures:
            raise LLMCallError(self.code, "上游抖了一下", retryable=self.retryable)
        return {"kind": "text", "content": "{}"}


def test_retry_provider_retries_only_retryable():
    ok = _Flaky(failures=2)
    p = rt._RetryProvider(ok, attempts=3, base_delay_s=0.0, sleep=lambda _s: None)
    p.chat([])
    assert ok.calls == 3, ok.calls

    auth = _Flaky(failures=1, code=ToolErrorCode.DENIED)
    p2 = rt._RetryProvider(auth, attempts=3, base_delay_s=0.0, sleep=lambda _s: None)
    with pytest.raises(LLMCallError):
        p2.chat([])
    assert auth.calls == 1, "鉴权错误重试没有意义，不该重试"


def test_retry_provider_respects_deadline_marker():
    """deadline 耗尽借 TIMEOUT 传播，但 retryable=False → 一次都不能重试。"""
    d = _Flaky(failures=9, code=ToolErrorCode.TIMEOUT, retryable=False)
    p = rt._RetryProvider(d, attempts=3, base_delay_s=0.0, sleep=lambda _s: None)
    with pytest.raises(LLMCallError):
        p.chat([])
    assert d.calls == 1, d.calls


class _BadPrimary:
    """主模型一律失败、备用模型成功——用来验证"降级"而不是"再失败一次"。"""

    def __init__(self):
        self.models: list[str | None] = []

    def chat(self, messages, **kw):
        self.models.append(kw.get("model"))
        if kw.get("model") != "backup-model":
            raise LLMCallError(ToolErrorCode.UNAVAILABLE, "主模型挂了")
        return {"kind": "text", "content": "{}"}


def test_retry_provider_falls_back_to_backup_model():
    """重试都失败后，用备用模型再试一次（而不是整次调查失败关闭）。"""
    inner = _BadPrimary()
    p = rt._RetryProvider(inner, attempts=2, base_delay_s=0.0,
                          fallback_model="backup-model", sleep=lambda _s: None)
    p.chat([], model="primary-model")
    assert inner.models == ["primary-model", "primary-model", "backup-model"], inner.models


def test_retry_provider_no_fallback_when_not_configured():
    inner = _Flaky(failures=10)
    p = rt._RetryProvider(inner, attempts=2, base_delay_s=0.0, sleep=lambda _s: None)
    with pytest.raises(LLMCallError):
        p.chat([], model="primary-model")
    assert inner.calls == 2, inner.calls


# ---------------------------------------------------------------- A1 额度硬熔断

def test_budget_charge_then_ensure_raises(monkeypatch):
    from data_dog import budget

    reset_state_store()
    monkeypatch.setenv("DD_LLM_BUDGET_TOKENS_PER_DAY", "100")
    budget.ensure("t1")                       # 还没花 → 放行
    budget.charge("t1", tokens=60, usd=0.0)
    budget.ensure("t1")                       # 60 < 100 → 放行
    budget.charge("t1", tokens=40)
    with pytest.raises(budget.BudgetExceeded) as ei:
        budget.ensure("t1")
    assert "token" in ei.value.detail
    snap = budget.snapshot("t1")
    assert snap["day"]["tokens"] == 100 and snap["exceeded"] is True


def test_budget_period_rolls_over_without_a_cron(monkeypatch):
    from data_dog import budget

    reset_state_store()
    monkeypatch.setenv("DD_LLM_BUDGET_USD_PER_DAY", "1")
    budget.charge("t2", usd=2.0, now=1_700_000_000.0)
    with pytest.raises(budget.BudgetExceeded):
        budget.ensure("t2", now=1_700_000_000.0)
    # 次日：同一 key，但周期变了 → 自动归零（不需要任何定时任务）
    budget.ensure("t2", now=1_700_000_000.0 + 86400)
    assert budget.snapshot("t2", now=1_700_000_000.0 + 86400)["day"]["usd"] == 0.0


def test_budget_does_not_gate_when_unset_or_not_enforced(client, monkeypatch):
    """默认（全 0）与 DD_LLM_BUDGET_ENFORCE=0 都不该拦人——零回归。"""
    from data_dog import budget

    monkeypatch.setenv("DD_LLM_BUDGET_TOKENS_PER_DAY", "10")
    monkeypatch.setenv("DD_LLM_BUDGET_ENFORCE", "0")
    budget.charge("tenant-real", tokens=999)
    budget.ensure("tenant-real")              # 只记不拦
    cid = _seed_candidate(client)
    r = client.post("/v1/investigations", headers=_Q, json={"candidate_id": cid})
    assert r.status_code == 200, r.text


def test_api_returns_429_when_budget_exhausted_and_leaves_a_run_log(client, monkeypatch):
    from data_dog import budget

    monkeypatch.setenv("DD_LLM_BUDGET_TOKENS_PER_DAY", "100")
    budget.charge("tenant-real", tokens=100)  # 额度先花光
    cid = _seed_candidate(client)
    r = client.post("/v1/investigations", headers=_Q, json={"candidate_id": cid})
    assert r.status_code == 429, r.text
    assert "上限" in r.json()["detail"]
    # 被额度拦下也是一次"运行"，技术员要能在运行日志里看到原因
    runs = client.get("/v1/investigations/runs", headers=_Q,
                      params={"status": "failed"}).json()["items"]
    assert runs and runs[0]["error_code"] == "budget_exceeded", runs


# ---------------------------------------------------------------- A2 队列准入

class _FullRunner:
    """队列已满的假 runner：submit 一旦被调用就说明"假的受理"发生了。

    stop() 转发给真 runner——服务的关停流程会调它，假 runner 不能把真池子漏在那。
    """

    alive = True

    def __init__(self, depth: int, real=None):
        self._depth = depth
        self._real = real

    def queue_depth(self) -> int:
        return self._depth

    def submit(self, **_kw):
        raise AssertionError("队列已满时不该入队（那是假的受理）")

    def stop(self) -> None:
        if self._real is not None:
            self._real.stop()


def test_api_returns_429_when_queue_full_before_creating_job(async_client, monkeypatch):
    monkeypatch.setenv("DD_INVESTIGATE_QUEUE_MAX", "3")
    real = rt._get_job_runner()
    monkeypatch.setattr(rt, "_get_job_runner",
                        lambda: _FullRunner(depth=3, real=real))
    cid = _seed_candidate(async_client)
    r = async_client.post("/v1/investigations", headers=_Q, json={"candidate_id": cid})
    assert r.status_code == 429, r.text
    assert r.headers.get("Retry-After") == "10"
    assert async_client.get("/v1/investigations/jobs", headers=_Q).json()["count"] == 0


# ---------------------------------------------------------------- A4 提交幂等

def test_idempotency_unit_states(monkeypatch):
    from data_dog.service import idempotency

    reset_state_store()
    key = "k-1"
    assert idempotency.reserve("t", key, ttl_s=60).state == "new"
    assert idempotency.reserve("t", key, ttl_s=60).state == "inflight"
    idempotency.complete("t", key, {"job_id": "job_x"}, ttl_s=60)
    d = idempotency.reserve("t", key, ttl_s=60)
    assert d.state == "done" and d.ref["job_id"] == "job_x"
    # 过期靠**读时判断**（不依赖后端 TTL 原语）：窗口内是 inflight，窗口外重新可占
    import time as _time
    base = _time.time()
    assert idempotency.reserve("t", "k-ttl", ttl_s=10, now=base).state == "new"
    assert idempotency.reserve("t", "k-ttl", ttl_s=10, now=base + 5).state == "inflight"
    assert idempotency.reserve("t", "k-ttl", ttl_s=10, now=base + 11).state == "new"
    # 释放：只对 running 生效，已完成的不许被删掉
    idempotency.release("t", key)
    assert idempotency.reserve("t", key, ttl_s=60).state == "done"


def test_sync_submit_is_idempotent(client):
    c = client
    cid = _seed_candidate(c)
    h = {**_Q, "Idempotency-Key": "sync-key-1"}
    r1 = c.post("/v1/investigations", headers=h, json={"candidate_id": cid})
    assert r1.status_code == 200, r1.text
    iid = r1.json()["investigation_id"]
    r2 = c.post("/v1/investigations", headers=h, json={"candidate_id": cid})
    assert r2.status_code == 200, r2.text
    assert r2.json()["idempotent_replay"] is True
    assert r2.json()["investigation_id"] == iid
    assert r2.json()["report"]["investigation_id"] == iid


def test_async_submit_is_idempotent(async_client):
    cid = _seed_candidate(async_client)
    h = {**_Q, "Idempotency-Key": "async-key-1"}
    r1 = async_client.post("/v1/investigations", headers=h,
                           json={"candidate_id": cid})
    assert r1.status_code == 202, r1.text
    job_id = r1.json()["job_id"]
    r2 = async_client.post("/v1/investigations", headers=h,
                           json={"candidate_id": cid})
    assert r2.status_code == 202, r2.text
    assert r2.json()["job_id"] == job_id
    assert r2.json()["idempotent_replay"] is True
    # 只应该有一条 job：重复提交不能重复花钱
    jobs = async_client.get("/v1/investigations/jobs", headers=_Q).json()
    assert jobs["count"] == 1, jobs


def test_without_idempotency_key_behaviour_is_unchanged(client):
    """不带该头 = 旧行为：每次提交都真跑（这里是零回归断言）。"""
    cid = _seed_candidate(client)
    r1 = client.post("/v1/investigations", headers=_Q, json={"candidate_id": cid})
    r2 = client.post("/v1/investigations", headers=_Q, json={"candidate_id": cid})
    assert r1.status_code == r2.status_code == 200
    assert "idempotent_replay" not in r2.json()
