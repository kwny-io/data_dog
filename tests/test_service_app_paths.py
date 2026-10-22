"""`service/app.py`（装配层）与 `service/routes/health.py`（探针）的分支测试。

为什么单开一份：这两个文件是"服务边界"的契约所在——统一错误体、鉴权失败关闭、
探针语义（存活 vs 就绪）、可选子系统起不来时**不拖垮 HTTP 服务**。已有
`tests/test_service.py` 覆盖了主干端点，但下列分支没被钉住，而它们恰恰决定
"故障时服务是优雅降级还是静默错误"：

- CORS 只在显式配置时挂载；请求上下文头（request-id / traceparent）必须回传；
- `DD_API_KEY` 一旦设置：非探针路径全部 401（失败关闭），并计入被拒计数；
- LLM 调用失败 → 504/503；未预期异常 → 统一 500 错误体且**不泄露内部细节**；
- lifespan 里迁移 / 流式 / worker / 快照 / 升级五个可选件任何一个起不来，
  服务仍须起来（由 `/readyz` 报未就绪，而不是进程崩溃）；
- `/readyz` 的就绪判定：数据库不可达 → 503；启用了流式/异步但没跑起来 → 503；
- `/metrics` 是 Prometheus 文本，采集库内行数失败**不让端点整体 500**。

全离线：用临时文件 SQLite + 关掉自动迁移，绝不碰仓库里的真实库文件。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from data_dog.errors import ToolErrorCode
from data_dog.providers.client import LLMCallError
from data_dog.service import runtime
from data_dog.service.routes import health as health_mod
from data_dog.storage.db import init_db, reset_engine

# `data_dog.service.__init__` 把 **FastAPI 实例**导成了 `app` 属性，属性查找会
# 盖住同名子模块；故必须走 import_module 才能拿到装配模块本身（要 patch 它）。
app_mod = importlib.import_module("data_dog.service.app")

_TRACE_ID = "a" * 32
_TRACE_SRC = f"00-{_TRACE_ID}-{'b' * 16}-01"


@pytest.fixture()
def sqlite_url(tmp_path, monkeypatch):
    """把服务指向独立临时库，并关掉自动迁移（不跑 alembic、不碰真实库）。"""
    url = f"sqlite:///{(tmp_path / 'svc.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    monkeypatch.setenv("DD_AUTO_MIGRATE", "0")
    reset_engine()
    init_db(url)
    yield url
    reset_engine()


@pytest.fixture()
def client(sqlite_url):
    """起一个 TestClient（触发 lifespan 启动/关停）。"""
    with TestClient(app_mod.create_app()) as c:
        yield c
    reset_engine()


# ---------------------------------------------------------------- CORS 分支


def test_cors_enabled_only_when_origins_configured(sqlite_url, monkeypatch):
    """配了 `DD_CORS_ORIGINS` 才挂 CORS；未配时不放行任何跨源来源。"""
    monkeypatch.setenv("DD_CORS_ORIGINS", "https://ui.example.com")
    with TestClient(app_mod.create_app()) as c:
        r = c.get("/healthz", headers={"Origin": "https://ui.example.com"})
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "https://ui.example.com"
        exposed = r.headers.get("access-control-expose-headers", "").lower()
        assert "x-request-id" in exposed and "traceparent" in exposed

    monkeypatch.delenv("DD_CORS_ORIGINS", raising=False)
    with TestClient(app_mod.create_app()) as c2:
        r2 = c2.get("/healthz", headers={"Origin": "https://evil.example.com"})
        assert "access-control-allow-origin" not in r2.headers
    print("PASS test_cors_enabled_only_when_origins_configured")


# ---------------------------------------------------------------- 请求上下文中间件


def test_request_context_echoes_request_id_and_traceparent(client):
    """客户端带的 X-Request-Id / traceparent 必须回传：上游可据此串起链路。"""
    r = client.get("/healthz", headers={"X-Request-Id": "rid-42",
                                        "traceparent": _TRACE_SRC})
    assert r.headers["X-Request-Id"] == "rid-42"
    parts = r.headers["traceparent"].split("-")
    assert len(parts) == 4
    assert parts[1] == _TRACE_ID           # 上游 trace-id 沿用
    assert parts[2] != "b" * 16            # span-id 必须新建，不照抄上游

    # 什么都没带 → 服务端生成两个头（否则日志无法关联）
    r2 = client.get("/healthz")
    assert r2.headers["X-Request-Id"]
    assert r2.headers["traceparent"].split("-")[1] != _TRACE_ID
    print("PASS test_request_context_echoes_request_id_and_traceparent")


# ---------------------------------------------------------------- 鉴权门


def test_api_key_guard_fails_closed_and_counts_rejections(client, monkeypatch):
    """设了 DD_API_KEY：非探针路径缺/错 key 一律 401，探针豁免，并计数被拒请求。"""
    monkeypatch.setenv("DD_API_KEY", "key-for-test-only")
    before = runtime._state.rejected_requests

    denied = client.get("/v1/candidates", headers={"X-Tenant-Id": "t1"})
    assert denied.status_code == 401
    assert denied.json() == {"ok": False, "error": "denied",
                            "detail": "缺少或错误的 X-API-Key",
                            "reason": "unauthorized"}
    wrong = client.get("/v1/candidates",
                       headers={"X-Tenant-Id": "t1", "X-API-Key": "nope"})
    assert wrong.status_code == 401
    assert runtime._state.rejected_requests == before + 2

    # 探针不校验（编排层探活不该被拦）
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    # 正确 key → 放行
    ok = client.get("/v1/candidates",
                    headers={"X-Tenant-Id": "t1", "X-API-Key": "key-for-test-only"})
    assert ok.status_code == 200
    print("PASS test_api_key_guard_fails_closed_and_counts_rejections")


# ---------------------------------------------------------------- 异常契约


def _app_with_routes(*routes) -> object:
    """造一个只含给定探针路由的 app（不污染全局 app）。"""
    app = app_mod.create_app()
    for path, handler in routes:
        app.get(path)(handler)
    return app


def test_llm_errors_map_to_504_and_503(sqlite_url):
    """LLM 超时 → 504；上游不可用 → 503；错误体是统一契约（不是裸 500）。"""
    def _timeout():
        raise LLMCallError(ToolErrorCode.TIMEOUT, "上游超时")

    def _unavailable():
        raise LLMCallError(ToolErrorCode.UNAVAILABLE, "上游不可用")

    app = _app_with_routes(("/boom-timeout", _timeout), ("/boom-unavailable", _unavailable))
    with TestClient(app) as c:
        rt = c.get("/boom-timeout")
        assert rt.status_code == 504
        assert rt.json() == {"ok": False, "error": "timeout", "detail": "上游超时"}
        ru = c.get("/boom-unavailable")
        assert ru.status_code == 503
        assert ru.json() == {"ok": False, "error": "unavailable", "detail": "上游不可用"}
    print("PASS test_llm_errors_map_to_504_and_503")


def test_unhandled_exception_returns_unified_body_without_leaking(sqlite_url):
    """未预期异常 → 500 统一错误体，detail 只有异常类型名，不泄内部消息。"""
    def _boom():
        raise RuntimeError("internal: sandbox path C:/tmp/secret")

    app = _app_with_routes(("/boom", _boom))
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom")
    assert r.status_code == 500
    assert r.json() == {"ok": False, "error": "execution_failed", "detail": "RuntimeError"}
    assert "secret" not in r.text
    print("PASS test_unhandled_exception_returns_unified_body_without_leaking")


def test_llm_error_handler_never_guesses_for_unexpected_type():
    """处理器只注册给 LLMCallError；万一收到别的类型也必须按兜底码返回，绝不猜。"""
    resp = asyncio.run(app_mod._llm_error_handler(None, ValueError("weird")))
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 500
    assert json.loads(resp.body) == {"ok": False, "error": "execution_failed",
                                     "detail": "ValueError"}
    print("PASS test_llm_error_handler_never_guesses_for_unexpected_type")


# ---------------------------------------------------------------- lifespan 可选件


def test_lifespan_runs_migration_when_enabled(sqlite_url, monkeypatch):
    """自动迁移开启时启动须跑 upgrade head，并记录版本号（打桩 alembic 本身）。"""
    calls: list[str] = []
    monkeypatch.setattr(app_mod.migrations, "auto_migrate_enabled", lambda: True)
    monkeypatch.setattr(app_mod.migrations, "upgrade_head",
                        lambda *a, **k: calls.append("upgrade"))
    monkeypatch.setattr(app_mod.migrations, "current_revision", lambda *a, **k: "rev-abc")

    with TestClient(app_mod.create_app()) as c:
        assert c.get("/healthz").status_code == 200
    assert calls == ["upgrade"]
    print("PASS test_lifespan_runs_migration_when_enabled")


def test_lifespan_skips_migration_when_disabled(monkeypatch, tmp_path):
    """`DD_AUTO_MIGRATE=0`（生产多副本）时**绝不**并发跑迁移，服务照常启动。"""
    monkeypatch.setenv("DD_DATABASE_URL",
                       f"sqlite:///{(tmp_path / 'svc.db').as_posix()}")
    monkeypatch.setenv("DD_AUTO_MIGRATE", "0")
    reset_engine()

    def _forbidden(*a, **k):
        raise AssertionError("DD_AUTO_MIGRATE=0 时不应自动迁移")

    monkeypatch.setattr(app_mod.migrations, "upgrade_head", _forbidden)
    try:
        with TestClient(app_mod.create_app()) as c:
            assert c.get("/healthz").status_code == 200
    finally:
        reset_engine()
    print("PASS test_lifespan_skips_migration_when_disabled")


def test_lifespan_optional_subsystems_fail_soft(sqlite_url, monkeypatch):
    """五个可选件（流式/worker/快照/升级 + 共享状态后端）全炸，HTTP 服务仍须可用。"""
    def _boom(name: str):
        def _raise() -> None:
            raise RuntimeError(f"{name} unavailable")
        return _raise

    for attr in ("_start_stream_if_enabled", "_start_job_runner_if_needed",
                 "_start_checkpoint_if_enabled", "_start_escalation_if_enabled",
                 "get_state_store"):
        monkeypatch.setattr(app_mod, attr, _boom(attr))

    with TestClient(app_mod.create_app()) as c:
        assert c.get("/healthz").status_code == 200
    print("PASS test_lifespan_optional_subsystems_fail_soft")


def test_stream_starts_and_stops_when_enabled(tmp_path, monkeypatch):
    """`DD_STREAM_ENABLED=1` 的 file 源真起后台消费线程，lifespan 退出时置停止位。"""
    if runtime._state.resolver is None:            # 缺厂商文档时绑定不可用，如实跳过
        pytest.skip(f"算子绑定不可用：{runtime._state.build_error}")

    log = tmp_path / "stream.jsonl"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("DD_DATABASE_URL",
                       f"sqlite:///{(tmp_path / 'svc.db').as_posix()}")
    monkeypatch.setenv("DD_AUTO_MIGRATE", "0")
    monkeypatch.setenv("DD_STREAM_ENABLED", "1")
    monkeypatch.setenv("DD_STREAM_SOURCE", "file")
    monkeypatch.setenv("DD_STREAM_FILE_PATH", str(log))
    reset_engine()
    try:
        with TestClient(app_mod.create_app()) as c:
            assert c.get("/healthz").status_code == 200
            runner, source, thread, stop = runtime._stream_handle()
            assert runner is not None
            assert source.name == "file"
            assert thread.name == "dd-stream-runner" and thread.is_alive()
            assert not stop.is_set()
        assert stop.is_set()                       # 关停：消费线程被唤醒退出
    finally:
        runtime._clear_stream()
        reset_engine()
    print("PASS test_stream_starts_and_stops_when_enabled")


def test_stream_refuses_to_start_without_operator_binding(sqlite_url, monkeypatch):
    """算子绑定不可用 → 流式拒绝启动（不静默收数据），但 HTTP 服务仍可用。"""
    monkeypatch.setenv("DD_STREAM_ENABLED", "1")
    monkeypatch.setattr(app_mod, "_state",
                        SimpleNamespace(new_plane=lambda: None, build_error="缺厂商文档"))

    with TestClient(app_mod.create_app()) as c:
        assert c.get("/healthz").status_code == 200
    assert runtime._stream_handle()[0] is None       # 没有"半启动"的 runner 残留
    print("PASS test_stream_refuses_to_start_without_operator_binding")


class _BrokenGauge:
    """打点即抛的指标替身。"""

    def set(self, *_a, **_k) -> None:
        raise RuntimeError("metrics backend down")


def test_stream_starts_even_if_metrics_label_fails(tmp_path, monkeypatch):
    """指标打点失败不能让流式接入起不来：可观测性不许反噬数据面。"""
    if runtime._state.resolver is None:
        pytest.skip(f"算子绑定不可用：{runtime._state.build_error}")

    log = tmp_path / "stream.jsonl"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("DD_DATABASE_URL",
                       f"sqlite:///{(tmp_path / 'svc.db').as_posix()}")
    monkeypatch.setenv("DD_AUTO_MIGRATE", "0")
    monkeypatch.setenv("DD_STREAM_ENABLED", "1")
    monkeypatch.setenv("DD_STREAM_SOURCE", "file")
    monkeypatch.setenv("DD_STREAM_FILE_PATH", str(log))
    monkeypatch.setattr(app_mod.metrics, "shard_enabled", _BrokenGauge())
    reset_engine()
    try:
        with TestClient(app_mod.create_app()) as c:
            assert c.get("/healthz").status_code == 200
            runner, source, thread, stop = runtime._stream_handle()
            assert runner is not None and source.name == "file"
            assert thread.is_alive()
        assert stop.is_set()
    finally:
        runtime._clear_stream()
        reset_engine()
    print("PASS test_stream_starts_even_if_metrics_label_fails")


def test_stream_stop_swallows_source_close_error(sqlite_url, monkeypatch):
    """关停时源 `close()` 抛错也必须吞掉并置停止位——否则进程退不干净。"""
    stop = threading.Event()

    def _boom_close() -> None:
        raise RuntimeError("close failed")

    runner = SimpleNamespace(stats={"flushed": 1})
    source = SimpleNamespace(close=_boom_close)
    monkeypatch.setattr(app_mod, "_stream_handle", lambda: (runner, source, None, stop))

    with TestClient(app_mod.create_app()) as c:
        assert c.get("/healthz").status_code == 200
        assert not stop.is_set()
    assert stop.is_set()
    print("PASS test_stream_stop_swallows_source_close_error")


# ---------------------------------------------------------------- /readyz 分支


def test_readyz_not_ready_when_database_unreachable(tmp_path, monkeypatch):
    """数据库连不上 → 503 not_ready（编排层据此摘流量，但不重启进程）。"""
    monkeypatch.setenv("DD_DATABASE_URL",
                       f"sqlite:///{(tmp_path / 'nope' / 'svc.db').as_posix()}")
    monkeypatch.setenv("DD_AUTO_MIGRATE", "0")
    reset_engine()
    try:
        with TestClient(app_mod.create_app()) as c:
            r = c.get("/readyz")
            assert r.status_code == 503
            body = r.json()
            assert body["status"] == "not_ready"
            assert body["checks"]["database"]["ok"] is False
            assert "error" in body["checks"]["database"]
            assert c.get("/healthz").status_code == 200     # 存活探针不受影响
    finally:
        reset_engine()
    print("PASS test_readyz_not_ready_when_database_unreachable")


def test_readyz_reports_migration_check_failure(client, monkeypatch):
    """迁移版本查不到（alembic 不可用/库异常）→ 如实标 ok=False，但不误报未就绪。"""
    def _boom(*a, **k):
        raise RuntimeError("no alembic")

    monkeypatch.setattr(health_mod.migrations, "current_revision", _boom)
    r = client.get("/readyz")
    assert r.status_code == 200
    checks = r.json()["checks"]
    assert checks["migration"]["ok"] is False
    assert "RuntimeError" in checks["migration"]["error"]
    print("PASS test_readyz_reports_migration_check_failure")


def test_readyz_reports_resolver_degraded(client, monkeypatch):
    """算子绑定缺失只降级接入，不让整机摘流量：resolver ok=False 但服务仍 ready。"""
    monkeypatch.setattr(health_mod, "_state",
                        SimpleNamespace(resolver=None, build_error="缺少厂商文档"))
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["checks"]["resolver"] == {"ok": False, "error": "缺少厂商文档"}
    print("PASS test_readyz_reports_resolver_degraded")


def test_readyz_not_ready_when_stream_enabled_but_not_started(client, monkeypatch):
    """启用了流式却没起消费 → 503（"看起来在跑、其实没收数据"是最危险的静默故障）。"""
    monkeypatch.setattr(health_mod.settings, "stream_enabled", lambda: True)
    monkeypatch.setattr(health_mod, "_stream_handle",
                        lambda: (None, None, None, None))
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["stream"] == {"ok": False, "error": "已启用但未启动"}
    print("PASS test_readyz_not_ready_when_stream_enabled_but_not_started")


def test_readyz_not_ready_when_stream_thread_dead(client, monkeypatch):
    """消费线程已退出（数据静默停收）→ 503。"""
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    runner = SimpleNamespace(stats={"flushed": 0})
    source = SimpleNamespace(name="file", coordination=lambda: "lease")
    monkeypatch.setattr(health_mod.settings, "stream_enabled", lambda: True)
    monkeypatch.setattr(health_mod, "_stream_handle",
                        lambda: (runner, source, dead, threading.Event()))
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["stream"] == {"ok": False, "error": "消费线程已退出"}
    print("PASS test_readyz_not_ready_when_stream_thread_dead")


def test_readyz_reports_stream_lease_holder_when_running(client, monkeypatch):
    """流式正常时回报来源/协调模式/统计与租约归属（一眼看出哪个副本在读）。"""
    stop = threading.Event()
    live = threading.Thread(target=stop.wait, name="probe-live-thread", daemon=True)
    live.start()
    runner = SimpleNamespace(stats={"candidates": 3})
    source = SimpleNamespace(name="file", coordination=lambda: "lease",
                             lease_holder="replica-a")
    monkeypatch.setattr(health_mod.settings, "stream_enabled", lambda: True)
    monkeypatch.setattr(health_mod, "_stream_handle",
                        lambda: (runner, source, live, stop))
    try:
        r = client.get("/readyz")
        assert r.status_code == 200
        stream = r.json()["checks"]["stream"]
        assert stream["ok"] is True
        assert stream["source"] == "file"
        assert stream["coordination"] == "lease"
        assert stream["stats"] == {"candidates": 3}
        assert stream["lease_holder"] == "replica-a"
    finally:
        stop.set()
        live.join(timeout=5.0)
    print("PASS test_readyz_reports_stream_lease_holder_when_running")


def test_readyz_not_ready_when_async_workers_missing(client, monkeypatch):
    """配了异步调查但 worker 池没跑 → 503（否则提交只会 503，实例应先被摘出）。"""
    monkeypatch.setattr(health_mod.settings, "investigate_async", lambda: True)
    monkeypatch.setattr(health_mod, "_get_job_runner", lambda: None)
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["investigation_workers"] == {
        "ok": False, "error": "异步调查 worker 池未运行"}
    print("PASS test_readyz_not_ready_when_async_workers_missing")


def test_readyz_reports_worker_queue_depth(client, monkeypatch):
    """worker 池在跑 → ready 并回报待办深度。"""
    runner = SimpleNamespace(alive=True, queue_depth=lambda: 4)
    monkeypatch.setattr(health_mod.settings, "investigate_async", lambda: True)
    monkeypatch.setattr(health_mod, "_get_job_runner", lambda: runner)
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["checks"]["investigation_workers"] == {"ok": True, "queue_depth": 4}
    print("PASS test_readyz_reports_worker_queue_depth")


def test_readyz_with_real_file_stream_reports_lease_holder(tmp_path, monkeypatch):
    """真实 file 源 + 租约时 `/readyz` 应 200 并回报租约持有者。

    这条曾经用 `xfail` 锁住一个真缺陷：`health.py` 把 `FileTailSource.lease_holder`
    这个**方法**当值塞进响应体，`JSONResponse.render` 抛 TypeError → `/readyz` 恒 500，
    于是 `DD_STREAM_ENABLED=1` 的 file 源部署**永远无法就绪**（编排层一直摘流量）。
    现在修好了（显式调用），故改成正常断言——留着 xfail 会让"修复"永远只表现为 XPASS。
    """
    if runtime._state.resolver is None:
        pytest.skip(f"算子绑定不可用：{runtime._state.build_error}")

    log = tmp_path / "stream.jsonl"
    log.write_text("", encoding="utf-8")
    monkeypatch.setenv("DD_DATABASE_URL",
                       f"sqlite:///{(tmp_path / 'svc.db').as_posix()}")
    monkeypatch.setenv("DD_AUTO_MIGRATE", "0")
    monkeypatch.setenv("DD_STREAM_ENABLED", "1")
    monkeypatch.setenv("DD_STREAM_SOURCE", "file")
    monkeypatch.setenv("DD_STREAM_FILE_PATH", str(log))
    reset_engine()
    # 先把表建好：`/readyz` 现在除了查"迁移版本到 head"，还会核对
    # `Base.metadata` 里的表是否**真的在库里**（见 health.py `_schema_present`）。
    # `DD_AUTO_MIGRATE=0` + 空库 = 一个真坏掉的部署，那时 503 才是对的。
    # 本用例要测的是"流式租约持有者能被报告"，所以按生产姿势（迁移由部署流程
    # 先跑完、应用侧 AUTO_MIGRATE=0）把库迁到 head。
    from data_dog.storage import migrations
    migrations.upgrade_head(f"sqlite:///{(tmp_path / 'svc.db').as_posix()}")
    try:
        app = app_mod.create_app()
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/readyz")
        assert r.status_code == 200, f"实际 {r.status_code}: {r.text[:200]}"
        assert r.json()["checks"]["stream"]["lease_holder"]
    finally:
        runtime._clear_stream()
        reset_engine()
    print("PASS test_readyz_with_real_file_stream_reports_lease_holder")


# ---------------------------------------------------------------- /metrics


def test_metrics_is_prometheus_text_with_db_rows(client):
    """/metrics 是 Prometheus 文本格式，且库内行数按表标签逐条上报。"""
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "# HELP" in body and "# TYPE" in body
    assert 'data_dog_db_rows{table="candidates"} 0.0' in body
    assert 'data_dog_db_rows{table="alerts"} 0.0' in body
    assert 'data_dog_db_rows{table="outbox_pending"} 0.0' in body
    assert "data_dog_auth_enabled 0.0" in body          # 未设 DD_API_KEY
    assert "data_dog_resolver_ready 1.0" in body
    print("PASS test_metrics_is_prometheus_text_with_db_rows")


def test_metrics_reflects_investigation_queue_depth(client, monkeypatch):
    """worker 池在跑时把待办深度刷进指标（容量问题的第一手信号）。"""
    monkeypatch.setattr(health_mod, "_get_job_runner",
                        lambda: SimpleNamespace(queue_depth=lambda: 7))
    body = client.get("/metrics").text
    assert "data_dog_investigation_queue_depth 7.0" in body
    print("PASS test_metrics_reflects_investigation_queue_depth")


def test_metrics_survives_db_collection_failure(client, monkeypatch):
    """采集库内行数失败只记日志，不让指标端点整体 500（否则抓取侧也瞎了）。"""
    def _boom(*a, **k):
        raise RuntimeError("count failed")

    monkeypatch.setattr(health_mod.repo, "count_rows", _boom)
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "data_dog_uptime_seconds" in r.text
    print("PASS test_metrics_survives_db_collection_failure")
