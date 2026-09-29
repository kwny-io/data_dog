"""在线服务层测试（FastAPI + TestClient）。

覆盖：存活/就绪探针、指标、鉴权失败关闭、接入→候选→落库、身份只认请求头、
查询过滤、批量上限。用临时文件 SQLite 隔离库。
"""
from __future__ import annotations

import json
import secrets

import pytest
from fastapi.testclient import TestClient

from data_dog.storage.db import init_db, reset_engine


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """把服务指向独立临时库，再起一个 TestClient（触发 startup 建表）。"""
    url = f"sqlite:///{(tmp_path / 'svc.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    reset_engine()
    init_db(url)

    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        yield c
    reset_engine()


def _raw(event_id="e1", temperature=95, **kw):
    base = dict(
        event_id=event_id, occurred_at="2026-09-13T10:00:00Z",
        received_at="2026-09-13T10:00:00.100Z", sequence=1,
        vendor="unitree", model="go2", firmware="3.2.1",
        protocol_version="motor-log-v2", component_hint="joint_motor.front_left_hip",
        payload_inline=json.dumps({"temperature": temperature, "current": 12.0, "mode": 0}),
    )
    base.update(kw)
    return base


_AUTH = {"X-Tenant-Id": "tenant-real", "X-Device-Sn": "device-go2-0001"}
# 查询端点的租户头（读也失败关闭：无租户头 → 401）
_Q = {"X-Tenant-Id": "tenant-real"}


def _auth_enabled(client, headers=None) -> str:
    """从 Prometheus 文本指标里读 auth_enabled 的值（"0"/"1"）。"""
    body = client.get("/metrics", headers=headers or {}).text
    for line in body.splitlines():
        # 无标签 gauge 输出形如 `data_dog_auth_enabled 0.0`（不带 `{`）
        if line.startswith("data_dog_auth_enabled "):
            return line.rsplit(" ", 1)[-1].split(".")[0]
    raise AssertionError("指标里没有 data_dog_auth_enabled")


# ---------------------------------------------------------------- 探针

def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readyz_reports_db(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"]["ok"] is True


def test_metrics_shape(client):
    """/metrics 返回 Prometheus 文本格式（可被 Prometheus 直接抓取）。"""
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    # 必须是 Prometheus 文本格式（HELP/TYPE 行），不是 JSON
    assert "# HELP" in body and "# TYPE" in body
    # 业务 / HTTP / 进程信息指标都应存在
    assert "data_dog_candidates_created_total" in body
    assert "data_dog_http_requests_total" in body
    assert "data_dog_db_rows" in body
    assert "data_dog_auth_enabled" in body
    assert "data_dog_resolver_ready" in body


# ---------------------------------------------------------------- 鉴权

def test_ingest_requires_identity_headers(client):
    r = client.post("/v1/telemetry", json={"events": [_raw()]})
    assert r.status_code == 401


def test_ingest_rejects_empty_events(client):
    r = client.post("/v1/telemetry", headers=_AUTH, json={"events": []})
    assert r.status_code == 422


def test_ingest_rejects_too_many_events(client):
    r = client.post("/v1/telemetry", headers=_AUTH,
                    json={"events": [_raw(f"e{i}") for i in range(1001)]})
    assert r.status_code == 422


# ---------------------------------------------------------------- 接入闭关

def test_ingest_produces_candidate_and_persists(client):
    r = client.post("/v1/telemetry", headers=_AUTH,
                    json={"events": [_raw("e1", temperature=96)]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["stats"]["ingested"] == 1
    assert body["stats"]["resolved"] == 1
    assert len(body["candidates"]) == 1
    assert body["candidates"][0]["severity_hint"] == "critical"
    assert body["persisted"]["candidates"] == 1

    # 落库可查
    q = client.get("/v1/candidates", headers=_AUTH)
    assert q.status_code == 200
    assert q.json()["count"] == 1


def test_body_identity_is_ignored(client):
    """正文谎报身份必须无效，实际身份以请求头为准。"""
    spoofed = _raw("e1", tenant_id="FAKE", device_sn="FAKE")
    r = client.post("/v1/telemetry", headers=_AUTH, json={"events": [spoofed]})
    assert r.status_code == 200
    items = client.get("/v1/candidates", headers=_Q).json()["items"]
    assert len(items) == 1
    assert items[0]["device_sn"] == "device-go2-0001"


def test_fail_closed_on_bad_firmware(client):
    """固件不在兼容范围 → 失败关闭，仍返回 200 + 统计，但不产候选。"""
    r = client.post("/v1/telemetry", headers=_AUTH,
                    json={"events": [_raw("e1", firmware="9.9.9")]})
    assert r.status_code == 200
    body = r.json()
    assert body["stats"]["rejected"] == 1
    assert body["candidates"] == []


def test_candidates_filter_by_device(client):
    client.post("/v1/telemetry", headers=_AUTH, json={"events": [_raw("e1")]})
    hit = client.get("/v1/candidates", headers=_Q,
                     params={"device_sn": "device-go2-0001"}).json()
    miss = client.get("/v1/candidates", headers=_Q,
                      params={"device_sn": "nope"}).json()
    assert hit["count"] == 1 and miss["count"] == 0


def test_ingest_is_idempotent_across_repeats(client):
    """同一条日志重复接入，候选按 ID 幂等 upsert，不产生重复行。"""
    for _ in range(3):
        client.post("/v1/telemetry", headers=_AUTH, json={"events": [_raw("e1")]})
    assert client.get("/v1/candidates", headers=_Q).json()["count"] == 1


# ---------------------------------------------------------------- 可选 API Key

def test_api_key_disabled_by_default(client):
    """不设 DD_API_KEY → 不校验（本地开发/评测行为不变）。"""
    assert _auth_enabled(client) == "0"
    assert client.get("/v1/candidates", headers=_Q).status_code == 200


def test_api_key_enforced_when_set(client, monkeypatch):
    # 密钥在运行时随机生成，源码里不出现任何密钥字面量
    key = secrets.token_urlsafe(24)
    wrong = secrets.token_urlsafe(24)
    monkeypatch.setenv("DD_API_KEY", key)

    # 探针豁免：编排层探活不该被拦
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200

    # 业务端点：缺密钥 / 错密钥 → 401
    assert client.get("/v1/candidates", headers=_Q).status_code == 401
    assert client.get("/v1/candidates",
                      headers={**_Q, "X-API-Key": wrong}).status_code == 401
    assert client.post("/v1/telemetry", headers=_AUTH,
                       json={"events": [_raw()]}).status_code == 401

    # 正确密钥 → 放行（业务头仍需齐全）
    ok = client.get("/v1/candidates", headers={**_Q, "X-API-Key": key})
    assert ok.status_code == 200

    ingest = client.post("/v1/telemetry",
                         headers={**_AUTH, "X-API-Key": key},
                         json={"events": [_raw()]})
    assert ingest.status_code == 200
    assert ingest.json()["stats"]["candidates"] == 1
    # /metrics 属受保护端点（开启鉴权后同样要带密钥）
    assert client.get("/metrics").status_code == 401
    assert _auth_enabled(client, headers={"X-API-Key": key}) == "1"


def test_blank_api_key_counts_as_unset(client, monkeypatch):
    """仅空白不算"已配置"——否则会误判为已开启鉴权。"""
    monkeypatch.setenv("DD_API_KEY", "   ")
    assert _auth_enabled(client) == "0"
    assert client.get("/v1/candidates", headers=_Q).status_code == 200


# ---------------------------------------------------------------- 限流（跨请求）

def test_rate_limit_applies_across_requests(client, monkeypatch):
    """限流必须跨请求生效。

    回归测试：曾经每个请求新建 DataPlane，而 DataPlane 又新建 TelemetryGateway，
    令牌桶随请求生灭 → 桶永远满、跨请求限流形同虚设（§16.3 声称做了实际没做）。
    这里把共享网关换成小容量桶，验证第 3 个请求被限。
    """
    import importlib

    from data_dog.data_plane.gateway import TelemetryGateway
    # 注意：模块末尾 `app = create_app()` 会把模块属性 app 覆盖成 FastAPI 实例，
    # 因此不能用 `from data_dog.service import app`，要用 importlib 取模块本体。
    svc = importlib.import_module("data_dog.service.app")

    # burst=2：本设备前 2 条放行，第 3 条起超限
    monkeypatch.setattr(svc._state, "gateway",
                        TelemetryGateway(rate_per_sec=0.0, burst=2))

    # 三次独立请求（每次都是新的 DataPlane，模拟真实请求隔离）
    codes = []
    for i in range(3):
        r = client.post("/v1/telemetry", headers=_AUTH,
                        json={"events": [_raw(event_id=f"rl{i}")]})
        codes.append(r.status_code)

    assert codes[0] == 200 and codes[1] == 200, f"前两条应放行，实际 {codes}"
    # 第 3 条应被限流：整条事件被失败关闭 → 无候选、stats.rejected=1
    r3 = client.post("/v1/telemetry", headers=_AUTH,
                     json={"events": [_raw(event_id="rl3")]})
    assert r3.status_code == 200, "限流是逐条失败关闭，不是整个请求失败"
    body = r3.json()
    assert body["stats"]["rejected"] == 1, "第 3 条应被限流拒绝"
    assert body["stats"]["rejected_by_reason"].get("rate_limited") == 1
    assert body["stats"]["candidates"] == 0


# ---------------------------------------------------------------- 统一错误契约

def test_error_code_http_mapping():
    """统一错误码 → HTTP 状态码映射必须是有限闭集且语义正确。"""
    from data_dog.errors import ToolErrorCode, http_status_for
    assert http_status_for(ToolErrorCode.DENIED) == 403
    assert http_status_for(ToolErrorCode.NOT_FOUND) == 404
    assert http_status_for(ToolErrorCode.RATE_LIMITED) == 429
    assert http_status_for(ToolErrorCode.TIMEOUT) == 504
    assert http_status_for(ToolErrorCode.UNAVAILABLE) == 503
    assert http_status_for(ToolErrorCode.INVALID_INPUT) == 422


def test_llm_error_returns_unified_contract(client, monkeypatch):
    """LLM 调用失败 → 统一错误体 + 正确状态码（超时 504 / 不可用 503）。"""
    import importlib

    from data_dog.errors import ToolErrorCode
    from data_dog.providers.client import LLMCallError
    svc = importlib.import_module("data_dog.service.app")

    def boom(*a, **kw):
        raise LLMCallError(ToolErrorCode.TIMEOUT, "LLM 调用超时（60.0s）")

    # 让接入路径走到模型调用：直接给 app 挂一个路由来触发异常处理器
    monkeypatch.setattr(svc._state, "new_plane", boom)

    r = client.post("/v1/telemetry", headers=_AUTH, json={"events": [_raw()]})
    assert r.status_code == 504, f"超时应映射 504，实际 {r.status_code}"
    body = r.json()
    assert body["ok"] is False and body["error"] == "timeout"


def test_auth_rejection_uses_unified_contract(client, monkeypatch):
    """鉴权拒绝也用统一错误体（不混两套契约）。"""
    monkeypatch.setenv("DD_API_KEY", "secret-key-value")
    r = client.get("/v1/candidates", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "denied"
    assert body["reason"] == "unauthorized"


# ---------------------------------------------------------------- 日志与追踪

def test_request_id_returned_and_propagated(client):
    """每个请求都有 request_id，并回传响应头（便于与上游链路对齐）。"""
    r = client.get("/v1/candidates", headers=_Q)
    rid = r.headers.get("X-Request-Id")
    assert rid, "响应必须带 X-Request-Id"
    assert len(rid) >= 8


def test_request_id_honors_client_supplied(client):
    """客户端透传的 X-Request-Id 应被沿用（跨服务链路对齐）。"""
    r = client.get("/v1/candidates",
                   headers={**_Q, "X-Request-Id": "upstream-abc-123"})
    assert r.headers.get("X-Request-Id") == "upstream-abc-123"


def test_traceparent_inherited_from_upstream(client):
    """上游 traceparent 的 trace-id 被沿用，且响应回传合法 traceparent。"""
    from data_dog.core.tracing import parse_traceparent
    trace = "e" * 32
    r2 = client.get("/v1/candidates",
                    headers={**_Q, "traceparent": f"00-{trace}-{'f' * 16}-01"})
    out = r2.headers.get("traceparent")
    assert out, "响应必须回传 traceparent"
    parsed = parse_traceparent(out)
    assert parsed is not None and parsed[0] == trace


def test_traceparent_generated_when_absent(client):
    """没带 traceparent 也要生成一个合法的回传。"""
    from data_dog.core.tracing import parse_traceparent
    r = client.get("/v1/candidates", headers=_Q)
    parsed = parse_traceparent(r.headers.get("traceparent", ""))
    assert parsed is not None


def test_cors_absent_by_default(client):
    """未设 DD_CORS_ORIGINS → 不挂 CORS 中间件，不带跨源头。"""
    r = client.get("/v1/candidates", headers=_Q)
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_cors_enabled_when_configured(monkeypatch, tmp_path):
    """设了 DD_CORS_ORIGINS → 预检带上 allow-origin（仅限白名单来源）。"""
    from fastapi.testclient import TestClient

    from data_dog.storage.db import init_db, reset_engine
    url = f"sqlite:///{(tmp_path / 'cors.db').as_posix()}"
    monkeypatch.setenv("DD_DATABASE_URL", url)
    monkeypatch.setenv("DD_CORS_ORIGINS", "https://app.example.com")
    reset_engine()
    init_db(url)
    from data_dog.service.app import create_app
    with TestClient(create_app()) as c:
        r = c.options("/v1/candidates", headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "GET",
        })
        assert r.headers.get("access-control-allow-origin") == "https://app.example.com"
    reset_engine()


def test_json_logging_emits_parseable_lines(capsys):
    """结构化日志必须是可解析的 JSON 行，且带 request_id。"""
    import json as _json

    from data_dog.core.logging import (
        clear_request_id,
        get_logger,
        set_request_id,
        setup_logging,
    )
    setup_logging(level="INFO", json_output=True, service="test-svc")
    log = get_logger("test.logger")
    set_request_id("rid-log-test")
    log.info("带上下文的日志", extra={"device_sn": "device-go2-0001"})
    clear_request_id()

    out = capsys.readouterr().out.strip().splitlines()
    assert out, "应有日志输出"
    rec = _json.loads(out[-1])           # 必须能解析成 JSON
    assert rec["msg"] == "带上下文的日志"
    assert rec["request_id"] == "rid-log-test"
    assert rec["device_sn"] == "device-go2-0001"
    assert rec["service"] == "test-svc"
    assert rec["level"] == "INFO"


def test_logging_human_readable_mode(capsys):
    """DD_LOG_JSON=0 时用人类可读格式（本地调试友好）。"""
    from data_dog.core.logging import get_logger, setup_logging
    setup_logging(level="INFO", json_output=False)
    get_logger("t").info("人读格式")
    out = capsys.readouterr().out
    assert "人读格式" in out and not out.strip().startswith("{")


# ---------------------------------------------------------------- 分页

def test_candidates_pagination(client):
    """查询端点必须分页，且返回 total/has_more（防一次拉全表）。"""
    # 灌 5 条不同设备的候选
    for i in range(5):
        client.post("/v1/telemetry",
                    headers={"X-Tenant-Id": "t1", "X-Device-Sn": f"dev-{i}"},
                    json={"events": [_raw(event_id=f"pg{i}")]})
    q = {"X-Tenant-Id": "t1"}

    r = client.get("/v1/candidates", headers=q, params={"limit": 2, "offset": 0})
    assert r.status_code == 200
    b = r.json()
    assert b["count"] == 2 and b["limit"] == 2 and b["offset"] == 0
    assert b["total"] == 5 and b["has_more"] is True

    # 最后一页：has_more=False
    r2 = client.get("/v1/candidates", headers=q, params={"limit": 10, "offset": 4})
    b2 = r2.json()
    assert b2["count"] == 1 and b2["has_more"] is False

    # 分页不重叠：offset=2 拿到的与 offset=0 不同
    r3 = client.get("/v1/candidates", headers=q, params={"limit": 2, "offset": 2})
    ids0 = {c["candidate_id"] for c in b["items"]}
    ids2 = {c["candidate_id"] for c in r3.json()["items"]}
    assert not (ids0 & ids2), "分页结果不应重叠"


def test_pagination_limit_is_capped(client):
    """limit 超上限应被拒（不得允许一次拉全表）。"""
    assert client.get("/v1/candidates", headers=_Q,
                      params={"limit": 99999}).status_code == 422
    assert client.get("/v1/candidates", headers=_Q,
                      params={"limit": 0}).status_code == 422
    assert client.get("/v1/candidates", headers=_Q,
                      params={"offset": -1}).status_code == 422


# ---------------------------------------------------------------- 指标

def test_ingest_increments_business_metrics(client):
    """接入必须反映到 Prometheus 指标（业务量可被监控）。"""
    client.post("/v1/telemetry", headers=_AUTH, json={"events": [_raw()]})

    body = client.get("/metrics").text
    assert "data_dog_telemetry_events_total" in body
    assert "data_dog_candidates_created_total" in body
    # 候选计数应 > 0（过温日志会命中）
    for line in body.splitlines():
        if line.startswith("data_dog_candidates_created_total "):
            assert float(line.rsplit(" ", 1)[-1]) >= 1.0
            break
    else:
        raise AssertionError("缺少 data_dog_candidates_created_total")


def test_metrics_path_label_is_route_template(client):
    """指标 path 标签必须是**路由模板**，不是实际 URL（控制标签基数）。"""
    client.get("/v1/candidates", headers=_Q, params={"limit": 1})
    body = client.get("/metrics").text
    # 应出现路由模板
    assert 'path="/v1/candidates"' in body
    # 不应出现带查询串的实际 URL
    assert "limit=1" not in body, "path 标签混入了查询串（基数会爆）"


def test_metrics_rejected_reason_labeled(client):
    """被失败关闭的事件按细分原因计数（限流等可被单独告警）。"""
    import importlib

    from data_dog.data_plane.gateway import TelemetryGateway
    svc = importlib.import_module("data_dog.service.app")
    old = svc._state.gateway
    try:
        svc._state.gateway = TelemetryGateway(rate_per_sec=0.0, burst=1)
        client.post("/v1/telemetry", headers=_AUTH, json={"events": [_raw(event_id="m1")]})
        client.post("/v1/telemetry", headers=_AUTH, json={"events": [_raw(event_id="m2")]})
        body = client.get("/metrics").text
        assert 'reason="rate_limited"' in body
        assert "data_dog_rate_limited_total" in body
    finally:
        svc._state.gateway = old


# ---------------------------------------------------------------- 多租户隔离

def test_query_requires_tenant_header(client):
    """读路径失败关闭：无 X-Tenant-Id → 401（租户是访问控制的一部分）。"""
    assert client.get("/v1/candidates").status_code == 401
    assert client.get("/v1/alerts").status_code == 401
    assert client.get("/v1/outbox").status_code == 401


def test_query_ignores_tenant_query_param(client):
    """`?tenant_id=` 一律被忽略：租户只能来自请求头。

    查询串可被链接/日志/浏览器历史携带与篡改，不是可信身份来源。这里用 A 租户
    写入、再以 A 头 + 谎报 `?tenant_id=B` 读取——必须仍读到 A 的数据（参数无效）。
    """
    client.post("/v1/telemetry", headers=_AUTH, json={"events": [_raw("e1")]})
    r = client.get("/v1/candidates", headers=_Q,
                   params={"tenant_id": "other-tenant"})
    assert r.status_code == 200
    assert r.json()["count"] == 1, "查询参数覆盖了租户（应按请求头取租户）"
    assert r.json()["items"][0]["tenant_id"] == "tenant-real"


def test_tenant_lists_are_isolated(client):
    """A 租户写入的候选/告警，B 租户查询时看不到。"""
    client.post("/v1/telemetry", headers=_AUTH, json={"events": [_raw("e1")]})
    # A 能看到
    assert client.get("/v1/candidates", headers=_Q).json()["count"] == 1
    # B 看不到（空列表，而非报错——不泄露"另一端有数据"）
    b = client.get("/v1/candidates", headers={"X-Tenant-Id": "tenant-other"}).json()
    assert b["count"] == 0 and b["total"] == 0 and b["items"] == []


# ---------------------------------------------------------------- 仪表盘聚合

def test_summary_empty_tenant(client):
    """空租户：计数为 0，但进程级事实（ready/auth）仍如实给出。"""
    r = client.get("/v1/summary", headers=_Q)
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] is True
    assert b["candidates_total"] == 0
    assert b["alerts_by_state"] == {}
    assert b["outbox_pending"] == 0
    assert isinstance(b["resolver_ready"], bool)
    assert isinstance(b["auth_enabled"], bool)
    assert b["uptime_s"] >= 0


def test_summary_requires_tenant(client):
    assert client.get("/v1/summary").status_code == 401


def test_summary_counts_tenant_scoped(client):
    """计数限定当前租户：本租户接入后总数上升，别租户仍见 0。"""
    _seed_candidate(client)
    b = client.get("/v1/summary", headers=_Q).json()
    assert b["candidates_total"] >= 1
    other = client.get("/v1/summary", headers={"X-Tenant-Id": "tenant-others"}).json()
    assert other["candidates_total"] == 0


# ---------------------------------------------------------------- 单资源端点

def _seed_candidate(client, event_id="e1", temperature=96):
    """接入一条过温日志，返回落库的 candidate_id。"""
    client.post("/v1/telemetry", headers=_AUTH,
                json={"events": [_raw(event_id, temperature=temperature)]})
    items = client.get("/v1/candidates", headers=_Q).json()["items"]
    return items[0]["candidate_id"]


def test_get_single_candidate(client):
    cid = _seed_candidate(client)
    r = client.get(f"/v1/candidates/{cid}", headers=_Q)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["item"]["candidate_id"] == cid
    assert body["item"]["severity_hint"] == "critical"


def test_resolve_candidate_refs_indexed(client):
    """候选的 metricwin ref 能被解析成**真实索引**（非 not_indexed 占位）。"""
    cid = _seed_candidate(client)
    r = client.get(f"/v1/candidates/{cid}/refs", headers=_Q)
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ok"] is True
    assert b["total"] >= 1 and b["resolved"] >= 1
    first = b["results"][0]
    assert first["ok"] is True
    assert first["source"] == "metricwin"
    assert first["payload"]["indexed"] is True
    assert first["payload"]["candidate_id"] == cid
    assert first["payload"]["component_ref"]  # 真反查到了部件


def test_resolve_candidate_refs_requires_tenant(client):
    cid = _seed_candidate(client)
    assert client.get(f"/v1/candidates/{cid}/refs").status_code == 401


def test_resolve_candidate_refs_cross_tenant_404(client):
    cid = _seed_candidate(client)
    r = client.get(f"/v1/candidates/{cid}/refs",
                   headers={"X-Tenant-Id": "tenant-other"})
    assert r.status_code == 404


def test_resolve_candidate_refs_unknown_404(client):
    assert client.get("/v1/candidates/cand_nope/refs",
                      headers=_Q).status_code == 404


def test_get_candidate_cross_tenant_404(client):
    """跨租户读单候选 → 404（不泄露"另一端存在该资源"）。"""
    cid = _seed_candidate(client)
    r = client.get(f"/v1/candidates/{cid}",
                   headers={"X-Tenant-Id": "tenant-other"})
    assert r.status_code == 404


def test_get_candidate_requires_tenant(client):
    cid = _seed_candidate(client)
    assert client.get(f"/v1/candidates/{cid}").status_code == 401


def test_candidate_evidence_endpoint(client):
    """证据端点：Agent 所见窗口指标 + 参照 + 厂商知识，全部来自落库遥测。"""
    cid = _seed_candidate(client)
    r = client.get(f"/v1/candidates/{cid}/evidence", headers=_Q)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["candidate_id"] == cid
    assert body["component_ref"] == "joint_motor.front_left_hip"
    # 标准名映射生效（temperature → temperature_c），与 Agent 证据同源
    assert "temperature_c" in body["metrics"]
    assert body["metrics"]["temperature_c"] == 96.0
    assert body["vendor_knowledge"], "关节电机应有厂商知识条目"


def test_candidate_evidence_reference_window_is_object_when_absent(client):
    """回归：无更早参照行时 `reference_window_metrics` 须为 `{}` 而非 `null`。

    前端类型声明为非空 `Record<string, number>` 且直接 `Object.keys(...)`——
    返回 `null` 会让候选详情页抛 TypeError 崩页。契约以对象空值表达"无参照"。
    """
    cid = _seed_candidate(client)
    r = client.get(f"/v1/candidates/{cid}/evidence", headers=_Q)
    assert r.status_code == 200
    assert r.json()["reference_window_metrics"] == {}


def test_candidate_evidence_cross_tenant_404(client):
    cid = _seed_candidate(client)
    r = client.get(f"/v1/candidates/{cid}/evidence",
                   headers={"X-Tenant-Id": "tenant-other"})
    assert r.status_code == 404


# ---------------------------------------------------------------- 调查端点

def _investigation_env(monkeypatch):
    """调查相关测试统一用离线确定性 provider（不调真实 LLM、结果可复现）。"""
    monkeypatch.setenv("DD_INVESTIGATE_OFFLINE", "1")


def test_create_investigation_offline(client, monkeypatch):
    """离线确定性调查：产出报告 + ReAct 轨迹 + 用量，并持久化。"""
    _investigation_env(monkeypatch)
    cid = _seed_candidate(client, temperature=96)

    r = client.post("/v1/investigations", headers=_Q,
                    json={"candidate_id": cid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["candidate_id"] == cid
    inv_id = body["investigation_id"]
    assert inv_id, "必须有 investigation_id"

    # 报告实体（对齐 §12.4）
    report = body["report"]
    assert report["disposition"] in (
        "confirmed", "rejected", "inconclusive", "needs_human_review")
    assert report["summary"], "报告必须有结论摘要"
    # 左髋过温 + 有近期固件变更 → 确定性 provider 给 needs_human_review
    assert report["disposition"] == "needs_human_review"
    # 回归：离线 provider 必须履行 prompt 声明的 event_type 契约，否则 alerting
    # 兜底成 unknown_event，所有故障共用同一 event_type（event_id/幂等键不再区分故障）
    assert report["event_type"] and report["event_type"] != "unknown_event"
    assert report["event_type"] == "joint_motor_temperature_high"

    # ReAct 轨迹：至少一条工具调用，工具名在闭集内
    assert body["trace"], "应有 ReAct 轨迹"
    names = {s["name"] for s in body["trace"]}
    assert names <= {"query_candidate_evidence", "query_vendor_knowledge",
                     "query_neighbor_events", "query_recent_changes"}

    # 人话结论
    assert body["verdict"] and body["verdict_detail"]

    # 持久化：可直接读回，不重跑 LLM
    r2 = client.get(f"/v1/investigations/{inv_id}", headers=_Q)
    assert r2.status_code == 200
    assert r2.json()["report"]["investigation_id"] == inv_id

    # 出现在历史报告列表里
    reports = client.get("/v1/reports", headers=_Q).json()
    assert reports["count"] == 1
    assert reports["items"][0]["investigation_id"] == inv_id


def test_create_investigation_unknown_candidate_404(client, monkeypatch):
    _investigation_env(monkeypatch)
    r = client.post("/v1/investigations", headers=_Q,
                    json={"candidate_id": "cand_does_not_exist"})
    assert r.status_code == 404


def test_create_investigation_cross_tenant_404(client, monkeypatch):
    """跨租户调查他人候选 → 404（不泄露存在性）。"""
    _investigation_env(monkeypatch)
    cid = _seed_candidate(client)
    r = client.post("/v1/investigations",
                    headers={"X-Tenant-Id": "tenant-other"},
                    json={"candidate_id": cid})
    assert r.status_code == 404


def test_create_investigation_requires_candidate_id(client, monkeypatch):
    _investigation_env(monkeypatch)
    r = client.post("/v1/investigations", headers=_Q, json={})
    assert r.status_code == 422


def test_create_investigation_requires_tenant(client, monkeypatch):
    _investigation_env(monkeypatch)
    r = client.post("/v1/investigations", json={"candidate_id": "x"})
    assert r.status_code == 401


def test_get_investigation_cross_tenant_404(client, monkeypatch):
    _investigation_env(monkeypatch)
    cid = _seed_candidate(client)
    inv_id = client.post("/v1/investigations", headers=_Q,
                         json={"candidate_id": cid}).json()["investigation_id"]
    r = client.get(f"/v1/investigations/{inv_id}",
                   headers={"X-Tenant-Id": "tenant-other"})
    assert r.status_code == 404


def test_reports_require_tenant(client):
    assert client.get("/v1/reports").status_code == 401


def test_reports_pagination(client, monkeypatch):
    """历史报告列表也分页（防一次拉全表）。

    每次换不同设备：candidate_id 由 device|component|signal 派生，同设备重复接入
    会命中同一候选、只产出一份报告（幂等，非 bug）。
    """
    _investigation_env(monkeypatch)
    for i in range(3):
        client.post("/v1/telemetry",
                    headers={"X-Tenant-Id": "tenant-real",
                             "X-Device-Sn": f"device-{i}"},
                    json={"events": [_raw(f"e{i}", temperature=96 + i)]})
    cids = [c["candidate_id"]
            for c in client.get("/v1/candidates", headers=_Q).json()["items"]]
    assert len(cids) == 3
    for cid in cids:
        client.post("/v1/investigations", headers=_Q, json={"candidate_id": cid})
    r = client.get("/v1/reports", headers=_Q, params={"limit": 2})
    b = r.json()
    assert b["count"] == 2 and b["has_more"] is True
    assert b["total"] == 3


# ---------------------------------------------------------------- 反馈回灌

def _seed_alert(event_id="alert-1", state="detected", tenant="tenant-real"):
    """直接往客户端临时库写一条告警（绕过 P3 状态机，聚焦回灌端点本身）。"""
    from data_dog.alerting.lifecycle import AlertEvent
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    with session_scope() as s:
        repo.save_alert(s, AlertEvent(
            event_id=event_id, idempotency_key=f"k-{event_id}",
            device_ref="device-go2-0001", component_ref="joint_motor.front_left_hip",
            event_type="thermal_overheat", state=state,
            severity="critical", confidence=0.9), tenant_id=tenant)


def test_feedback_false_positive_rejects_detected(client):
    """detected 告警收到「误报」→ 转 rejected，并落一条反馈。"""
    _seed_alert(event_id="a-fp", state="detected")
    r = client.post("/v1/feedback", headers=_Q,
                    json={"event_id": "a-fp", "kind": "false_positive",
                          "note": "传感器漂移"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["state_applied"] == "rejected"
    assert b["state_now"] == "rejected"
    assert b["alert"]["state"] == "rejected"

    fb = client.get("/v1/alerts/a-fp/feedback", headers=_Q).json()
    assert fb["count"] == 1 and fb["items"][0]["kind"] == "false_positive"


def test_feedback_repaired_closes_confirmed(client):
    """confirmed 告警收到「已修」→ 一步转 closed（confirmed→closed 是合法边）。"""
    _seed_alert(event_id="a-rp", state="confirmed")
    r = client.post("/v1/feedback", headers=_Q,
                    json={"event_id": "a-rp", "kind": "repaired"})
    b = r.json()
    assert b["state_applied"] == "closed"
    assert b["state_now"] == "closed"


def test_feedback_repaired_detected_not_forced(client):
    """detected 无直达 closed 的边：repaired 只存档、不改状态（如实拒绝，不臆造中间态）。"""
    _seed_alert(event_id="a-rp-det", state="detected")
    r = client.post("/v1/feedback", headers=_Q,
                    json={"event_id": "a-rp-det", "kind": "repaired"})
    b = r.json()
    assert b["state_applied"] is None
    assert b["state_now"] == "detected"


def test_feedback_no_action_keeps_state(client):
    """no_action 只存档、不改状态（state_applied 为 null）。"""
    _seed_alert(event_id="a-na", state="detected")
    r = client.post("/v1/feedback", headers=_Q,
                    json={"event_id": "a-na", "kind": "no_action"})
    b = r.json()
    assert b["state_applied"] is None
    assert b["state_now"] == "detected"
    # 反馈仍落库（可审计）
    assert client.get("/v1/alerts/a-na/feedback", headers=_Q).json()["count"] == 1


def test_feedback_illegal_transition_not_forced(client):
    """closed 是终态：误报反馈不能把它拉回 rejected —— 如实拒绝，不改状态。"""
    _seed_alert(event_id="a-closed", state="closed")
    r = client.post("/v1/feedback", headers=_Q,
                    json={"event_id": "a-closed", "kind": "false_positive"})
    b = r.json()
    assert b["state_applied"] is None
    assert b["state_now"] == "closed"


def test_feedback_unknown_kind_422(client):
    _seed_alert(event_id="a-bad", state="detected")
    r = client.post("/v1/feedback", headers=_Q,
                    json={"event_id": "a-bad", "kind": "whatever"})
    assert r.status_code == 422


def test_feedback_missing_fields_422(client):
    r = client.post("/v1/feedback", headers=_Q, json={"kind": "repaired"})
    assert r.status_code == 422


def test_feedback_requires_tenant(client):
    assert client.post("/v1/feedback", json={"event_id": "x",
                                             "kind": "repaired"}).status_code == 401


def test_feedback_cross_tenant_404(client):
    """别租户的告警：对当前租户不可见 → 404，且不泄露存在性。"""
    _seed_alert(event_id="a-other", state="detected", tenant="tenant-other")
    r = client.post("/v1/feedback", headers=_Q,
                    json={"event_id": "a-other", "kind": "repaired"})
    assert r.status_code == 404
    assert client.get("/v1/alerts/a-other/feedback", headers=_Q).status_code == 404


# ---------------------------------------------------------------- P3 接进服务

def test_investigation_creates_alert(client, monkeypatch):
    """调查成功后 P3 落库产告警——让 /v1/alerts、/v1/summary 在真实运行里有数据。

    此前后端从不产告警（save_alert 只在测试里调），反馈/告警端点全是悬空的。
    """
    _investigation_env(monkeypatch)
    cid = _seed_candidate(client, temperature=96)

    body = client.post("/v1/investigations", headers=_Q,
                       json={"candidate_id": cid}).json()
    # 端点如实回报 P3 结果
    assert body["alerting"]["reported"] is True, body["alerting"]
    assert body["alerting"]["event_id"]
    assert body["alerting"]["event_state"] == "confirmed"
    assert body["alerting"]["new"] is True

    # 告警可查，且租户作用域内可见
    alerts = client.get("/v1/alerts", headers=_Q).json()
    assert alerts["count"] == 1
    ev = alerts["items"][0]
    assert ev["investigation_id"] == body["investigation_id"]
    # 设备引用是不透明派生，不含明文 SN
    assert "device-go2-0001" not in ev["device_ref"]
    assert ev["device_ref"].startswith("device-")

    # summary 的 alerts_by_state 不再恒空
    summ = client.get("/v1/summary", headers=_Q).json()
    assert summ["alerts_by_state"].get("confirmed") == 1


def test_investigation_repeat_is_update_not_new(client, monkeypatch):
    """同一候选重复调查 → 同一 event_id（Episode 语义），是**更新**不是新建。"""
    _investigation_env(monkeypatch)
    cid = _seed_candidate(client, temperature=96)

    a = client.post("/v1/investigations", headers=_Q,
                    json={"candidate_id": cid}).json()["alerting"]
    b = client.post("/v1/investigations", headers=_Q,
                    json={"candidate_id": cid}).json()["alerting"]
    assert a["event_id"] == b["event_id"], "同一 Episode 必须复用 event_id"
    assert b["new"] is False, "第二次是更新"
    assert b["event_revision"] > a["event_revision"], "revision 递增"
    # 只有一条告警（更新而非新建）
    assert client.get("/v1/alerts", headers=_Q).json()["count"] == 1


def test_investigation_outbox_persisted(client, monkeypatch):
    """投递落库：outbox 有条目且状态为 delivered（重启可查，非进程内）。"""
    _investigation_env(monkeypatch)
    cid = _seed_candidate(client, temperature=96)
    body = client.post("/v1/investigations", headers=_Q,
                       json={"candidate_id": cid}).json()
    assert body["alerting"]["delivered"] is True
    outbox = client.get("/v1/outbox", headers=_Q,
                        params={"status": "delivered"}).json()
    assert outbox["count"] == 1


# ---------------------------------------------------------------- 反馈回灌评估

def _seed_report(inv_id, disposition, tenant="tenant-real"):
    """直接写一份调查报告（精确控制 disposition，用于评估口径测试）。"""
    from data_dog.schemas.events import InvestigationReport
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    with session_scope() as s:
        repo.save_report(s, InvestigationReport(
            investigation_id=inv_id, disposition=disposition,
            confidence=0.9, summary="seed"), tenant_id=tenant)


def _seed_alert_full(event_id, state, inv_id, tenant="tenant-real"):
    """写一条**带 investigation_id** 的告警（评估关联经它回到报告）。"""
    from data_dog.alerting.lifecycle import AlertEvent
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    with session_scope() as s:
        repo.save_alert(s, AlertEvent(
            event_id=event_id, idempotency_key=f"k-{event_id}",
            device_ref="device-opaque", component_ref="joint_motor.front_left_hip",
            event_type="thermal", state=state, severity="critical",
            confidence=0.9, investigation_id=inv_id), tenant_id=tenant)


def test_eval_feedback_classifies_agree_and_error(client):
    """评估端点把 (人工 kind, Agent 裁决) 归类为一致/误报/漏报并算率。"""
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    # 场景：inv-confirmed 报告 confirmed + 告警 + 人工 repaired → agree
    #       inv-rejected 报告 rejected + 告警 + 人工 false_positive → agree
    #       inv-fp 报告 confirmed + 告警 + 人工 false_positive → false_positive
    _seed_report("inv-c", "confirmed")
    _seed_report("inv-r", "rejected")
    _seed_report("inv-fp", "confirmed")
    _seed_alert_full("a-c", "confirmed", "inv-c")
    _seed_alert_full("a-r", "rejected", "inv-r")
    _seed_alert_full("a-fp", "confirmed", "inv-fp")
    with session_scope() as s:
        repo.save_feedback(s, tenant_id="tenant-real", event_id="a-c", kind="repaired")
        repo.save_feedback(s, tenant_id="tenant-real", event_id="a-r",
                           kind="false_positive")
        repo.save_feedback(s, tenant_id="tenant-real", event_id="a-fp",
                           kind="false_positive")

    b = client.get("/v1/eval/feedback", headers=_Q).json()
    s = b["summary"]
    assert s["labeled"] == 3
    assert s["agree"] == 2
    assert s["false_positive"] == 1
    assert s["false_negative"] == 0
    assert s["agreement_rate"] == round(2 / 3, 3)
    # 明细行带上 Agent 裁决，可下钻
    assert {r["event_id"] for r in b["rows"]} == {"a-c", "a-r", "a-fp"}


def test_eval_feedback_missing_report_is_unlabeled(client):
    """告警无对应报告（调查失败/别处产生）→ 如实归 unlabeled，不臆造裁决。"""
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    _seed_alert("a-orphan", state="detected")   # 无 investigation_id
    with session_scope() as s:
        repo.save_feedback(s, tenant_id="tenant-real", event_id="a-orphan",
                           kind="false_positive")
    s = client.get("/v1/eval/feedback", headers=_Q).json()["summary"]
    assert s["total"] == 1
    assert s["labeled"] == 0
    assert s["unlabeled"] == 1


def test_eval_feedback_requires_tenant(client):
    assert client.get("/v1/eval/feedback").status_code == 401


def test_eval_feedback_cross_tenant_isolated(client):
    """别租户的反馈不可见：只统计本租户。"""
    from data_dog.storage import repository as repo
    from data_dog.storage.db import session_scope
    _seed_report("inv-x", "confirmed", tenant="tenant-other")
    _seed_alert_full("a-x", "confirmed", "inv-x", tenant="tenant-other")
    with session_scope() as s:
        repo.save_feedback(s, tenant_id="tenant-other", event_id="a-x",
                           kind="repaired")
    s = client.get("/v1/eval/feedback", headers=_Q).json()["summary"]
    assert s["total"] == 0


# ---------------------------------------------------------------- 用户厂商文档

def test_knowledge_import_and_search(client):
    """导入用户文档 → 列表可见 → 词法检索命中。"""
    r = client.post("/v1/knowledge/documents", headers=_Q,
                    json={"source_name": "go2-manual.txt",
                          "text": "电机温度超过 90°C 触发过热保护。\n\n电池 SOC 低电压告警。"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["ok"] is True and b["document_id"].startswith("kdoc_")
    assert b["chunk_count"] >= 1

    docs = client.get("/v1/knowledge/documents", headers=_Q).json()
    assert docs["count"] == 1
    assert docs["items"][0]["source_name"] == "go2-manual.txt"

    # 词法检索（英文词）
    s = client.get("/v1/knowledge/search", headers=_Q,
                   params={"q": "SOC 电池"}).json()
    assert s["count"] >= 1
    assert any("SOC" in h["text"] for h in s["hits"])
    assert s["hits"][0]["source_name"] == "go2-manual.txt"


def test_knowledge_import_idempotent(client):
    """同内容重复导入 → 同 document_id（内容寻址），不产生重复文档。"""
    body = {"source_name": "a.txt", "text": "unique content here"}
    a = client.post("/v1/knowledge/documents", headers=_Q, json=body).json()
    b = client.post("/v1/knowledge/documents", headers=_Q, json=body).json()
    assert a["document_id"] == b["document_id"]
    assert client.get("/v1/knowledge/documents", headers=_Q).json()["count"] == 1


def test_knowledge_requires_tenant(client):
    assert client.get("/v1/knowledge/documents").status_code == 401
    assert client.get("/v1/knowledge/search", params={"q": "x"}).status_code == 401
    assert client.post("/v1/knowledge/documents",
                       json={"text": "x"}).status_code == 401


def test_knowledge_import_empty_text_422(client):
    r = client.post("/v1/knowledge/documents", headers=_Q,
                    json={"source_name": "x", "text": "   "})
    assert r.status_code == 422


def test_knowledge_cross_tenant_isolated(client):
    """租户隔离：t1 导入的文档，t2 检索不到、列表为空。"""
    client.post("/v1/knowledge/documents", headers=_Q,
                json={"source_name": "private.txt", "text": "secret motor spec"})
    other = {"X-Tenant-Id": "tenant-other"}
    assert client.get("/v1/knowledge/documents", headers=other).json()["count"] == 0
    hits = client.get("/v1/knowledge/search", headers=other,
                      params={"q": "motor"}).json()
    assert hits["count"] == 0


def test_knowledge_delete(client):
    doc = client.post("/v1/knowledge/documents", headers=_Q,
                      json={"text": "to be deleted"}).json()
    did = doc["document_id"]
    assert client.delete(f"/v1/knowledge/documents/{did}",
                         headers=_Q).status_code == 200
    assert client.get("/v1/knowledge/documents", headers=_Q).json()["count"] == 0
    # 再删 → 404
    assert client.delete(f"/v1/knowledge/documents/{did}",
                         headers=_Q).status_code == 404


def test_feedback_unknown_alert_404(client):
    r = client.post("/v1/feedback", headers=_Q,
                    json={"event_id": "nope", "kind": "repaired"})
    assert r.status_code == 404
