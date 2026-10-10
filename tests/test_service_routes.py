"""HTTP 路由表与鉴权的护栏（拆分 create_app 之后新增）。

为什么需要这两类断言
--------------------
1) **路由快照（含顺序）**：`service/app.py` 原先内联 42 个端点、1187 行，现按域拆到
   `service/routes/`。这种搬迁有两类难察觉的错误：
   - 少搬/改错**端点** → 服务照常启动，只是某个 URL 悄悄变 404；
   - 打乱**注册顺序** → FastAPI 按注册顺序匹配，带模板的路径一旦排到字面量路径之前，
     就会把字面量当成路径参数吞掉。
   第二类真实发生过：拆分时 `/v1/investigations/{investigation_id}` 被排到
   `/v1/investigations/jobs` 前面，`GET /v1/investigations/jobs` 于是返回
   404「调查报告不存在」（全量测试抓出来的）。故这里同时钉住**集合**与**顺序**。

2) **鉴权可反射**：鉴权改成 `Depends(...)` 后，它会出现在 `route.dependant` 里，
   于是"新端点忘了挂鉴权"能被反射发现，而不是靠人肉 review。改造前 42 个端点各自
   在函数体里手写 `tenant = _tenant_required(...)`，漏写只会表现为越权，测试也不会红。
"""
from __future__ import annotations

from fastapi.routing import APIRoute

from data_dog.service.app import create_app
from data_dog.service.deps import (
    require_ops_token,
    require_ops_token_scope,
    require_tenant,
    require_tenant_and_device,
    require_tenant_or_ops,
)

# 快照：42 条端点的 (method, path) 与**注册顺序**（有意增删/重排时同步更新它）。
ROUTES = (
    ("GET", "/healthz"),
    ("GET", "/readyz"),
    ("GET", "/metrics"),
    ("POST", "/v1/state/checkpoint"),
    ("GET", "/v1/state/checkpoints"),
    ("GET", "/v1/state/diff"),
    ("POST", "/v1/telemetry"),
    ("GET", "/v1/summary"),
    ("GET", "/v1/candidates"),
    ("GET", "/v1/alerts"),
    ("GET", "/v1/outbox"),
    ("GET", "/v1/candidates/{candidate_id}"),
    ("GET", "/v1/candidates/{candidate_id}/evidence"),
    ("GET", "/v1/candidates/{candidate_id}/refs"),
    ("GET", "/v1/reports"),
    ("GET", "/v1/investigations/jobs"),
    ("GET", "/v1/investigations/jobs/{job_id}"),
    ("POST", "/v1/investigations/jobs/{job_id}/cancel"),
    ("GET", "/v1/investigations/{investigation_id}"),
    ("POST", "/v1/investigations"),
    ("POST", "/v1/feedback"),
    ("GET", "/v1/alerts/{event_id}/feedback"),
    ("GET", "/v1/eval/feedback"),
    ("POST", "/v1/knowledge/documents"),
    ("GET", "/v1/knowledge/documents"),
    ("GET", "/v1/knowledge/search"),
    ("DELETE", "/v1/knowledge/documents/{document_id}"),
    ("POST", "/v1/changes/ingest"),
    ("GET", "/v1/changes"),
    ("POST", "/v1/operators"),
    ("GET", "/v1/operators"),
    ("GET", "/v1/operators/{operator_id}/{version}"),
    ("POST", "/v1/operators/{operator_id}/{version}/transition"),
    ("POST", "/v1/operators/{operator_id}/{version}/rollback"),
    ("POST", "/v1/operators/{operator_id}/{version}/canary"),
    ("POST", "/v1/operators/{operator_id}/{version}/watchdog"),
    ("GET", "/v1/devices/{device_sn}/forecast"),
    ("GET", "/v1/candidates/{candidate_id}/forecast"),
    ("POST", "/v1/alerts/{event_id}/ack"),
    ("GET", "/v1/oncall"),
    ("PUT", "/v1/oncall"),
    ("GET", "/v1/notifications"),
    ("GET", "/v1/audit"),
    ("GET", "/v1/keys"),
    ("GET", "/v1/tenant/export"),
    ("POST", "/v1/tenant/purge"),
)

AUTH_DEPS = {require_tenant, require_tenant_and_device, require_ops_token,
             require_ops_token_scope, require_tenant_or_ops}


def _api_routes() -> list[APIRoute]:
    """按注册顺序展平 `app.routes` 里的端点（跨 FastAPI 版本）。

    不同版本对 `include_router` 的内部表示不同：有的把 `APIRoute` 直接铺进
    `app.routes`，0.141 则包一层 `_IncludedRouter`（只暴露 `original_router`）。
    两种都认；最后那条 assert 很关键——遍历方式一旦与新版本不匹配就会得到**空集**，
    那时"每个路由都挂了鉴权"与"顺序正确"都会**假绿**（本次拆分就踩过这个坑）。
    """
    out: list[APIRoute] = []
    for r in create_app().routes:
        if isinstance(r, APIRoute):
            out.append(r)
            continue
        for attr in ("router", "original_router"):
            inner = getattr(r, attr, None)
            if inner is not None and getattr(inner, "routes", None):
                out.extend(x for x in inner.routes if isinstance(x, APIRoute))
                break
    assert out, "没反射到任何端点——遍历方式与当前 FastAPI 版本不匹配（会让本文件假绿）"
    return out


def _route_pairs() -> list[tuple[str, str]]:
    return [(sorted(r.methods)[0], r.path) for r in _api_routes()]


def _dependency_calls(route: APIRoute) -> set:
    """递归收集该路由依赖树里的所有可调用对象（含子依赖）。"""
    out: set = set()
    stack = [route.dependant]
    while stack:
        d = stack.pop()
        if d.call is not None:
            out.add(d.call)
        stack.extend(d.dependencies)
    return out


def test_route_table_matches_snapshot():
    """42 条端点一条不多、一条不少，且**注册顺序**与快照一致。"""
    actual = _route_pairs()
    expected = list(ROUTES)
    if actual != expected:
        missing = [p for p in expected if p not in actual]
        extra = [p for p in actual if p not in expected]
        first = next((i for i, (a, b) in enumerate(zip(actual, expected, strict=False))
                      if a != b), min(len(actual), len(expected)))
        raise AssertionError(
            "路由表与快照不一致。\n"
            f"  缺失: {missing}\n"
            f"  多出: {extra}\n"
            f"  首个位置差异(下标 {first}): 实际 {actual[first:first + 1]} "
            f"vs 期望 {expected[first:first + 1]}\n"
            "  注意：路由**顺序**在 FastAPI 里是语义（字面量必须先于模板路径）。")
    print("PASS test_route_table_matches_snapshot")


def test_literal_route_wins_over_template():
    """字面量路径必须先于会吞掉它的模板路径注册。

    具体案例：`/v1/investigations/jobs`（字面量）与
    `/v1/investigations/{investigation_id}`（模板）。模板若在前，
    `GET /v1/investigations/jobs` 会被解析成 id="jobs" → 404「调查报告不存在」。
    """
    paths = [r.path for r in _api_routes()]
    jobs = paths.index("/v1/investigations/jobs")
    tmpl = paths.index("/v1/investigations/{investigation_id}")
    assert jobs < tmpl, (
        "模板路由排到了字面量路由之前：/v1/investigations/jobs 会被当成 investigation_id")
    print("PASS test_literal_route_wins_over_template")


def test_every_v1_route_declares_auth():
    """每个 /v1 端点都必须挂**声明式**鉴权依赖（租户，或运维 token）。

    这是把"别忘写鉴权"从纪律变成检查：新端点漏挂 → 这里直接红。
    """
    missing = [f"{sorted(r.methods)[0]} {r.path}" for r in _api_routes()
               if r.path.startswith("/v1") and not (_dependency_calls(r) & AUTH_DEPS)]
    assert not missing, f"这些 /v1 端点没有声明式鉴权依赖：{missing}"
    print("PASS test_every_v1_route_declares_auth")


def test_probes_and_metrics_stay_public():
    """探针与 /metrics 必须不挂鉴权：编排层探活不带业务凭证，挂了会一直摘流量。"""
    routes = _api_routes()
    probes = {r.path for r in routes if not r.path.startswith("/v1")}
    assert probes == {"/healthz", "/readyz", "/metrics"}
    for r in routes:
        if r.path in probes:
            assert not (_dependency_calls(r) & AUTH_DEPS), f"{r.path} 不该挂鉴权依赖"
    print("PASS test_probes_and_metrics_stay_public")
