"""HTTP 服务入口（FastAPI）：把 P2 数据面暴露为可长期运行的服务。

启动：
    uvicorn data_dog.service.app:app --host 0.0.0.0 --port 8000
    或  python -m data_dog.service

配置（环境变量）：
    DD_DATABASE_URL   数据库连接串（默认本地 SQLite；生产指向 PostgreSQL）
    DD_SERVICE_NAME   服务名（默认 data-dog）
    DD_SERVICE_PORT   监听端口（默认 8000；仅 `python -m` 启动时生效）

探针语义：
    /healthz  存活：进程还在就 200，不查依赖（避免依赖抖动导致容器被反复重启）。
    /readyz   就绪：探测数据库连通性；不通返回 503，编排层据此摘流量。
"""
from __future__ import annotations

import hmac
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import settings
from ..agent.loop import AgentLoop
from ..core import metrics
from ..core.logging import (
    clear_request_id,
    get_logger,
    set_request_id,
    setup_logging,
)
from ..core.tracing import clear_trace, start_trace
from ..data_plane.gateway import TelemetryGateway
from ..data_plane.p2_demo import _detector_config
from ..data_plane.pipeline import DataPlane, build_demo_resolver
from ..errors import ToolErrorCode, http_status_for
from ..presentation import derive_verdict
from ..providers.client import LLMCallError
from ..providers.config import load_config
from ..providers.offline_demo import DeterministicEvidenceProvider
from ..schemas.events import CandidateEvent, EvidenceSummary
from ..storage import migrations
from ..storage import models as M
from ..storage import repository as repo
from ..storage.db import database_url as _database_url
from ..storage.db import get_engine, session_scope
from ..tools.investigation import InvestigationTools
from .evidence import RepoEvidenceBridge

_log = get_logger(__name__)


def _route_template(request) -> str:
    """取**路由模板**作为指标标签，而非实际 URL。

    实际 URL 带查询串/路径参数会把标签基数撑爆（Prometheus 大忌，
    每个不同 URL 都会生成一条新时间序列）。优先用 FastAPI 匹配到的
    路由模板（如 `/v1/candidates`），取不到则退回 `unmatched`。
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or "unmatched"


_VERSION = "0.1.0"
_STARTED_AT = time.time()


# ------------------------------------------------------------------ 服务状态

class ServiceState:
    """进程内状态：构建一次解析器/绑定/网关，复用到每个请求。

    - resolver/绑定是无状态只读的，构建一次即可。
    - **gateway 必须跨请求共享**：它的令牌桶是限流状态的载体（§16.3）。若每个
      请求新建 gateway，桶会随请求生灭、永远满桶，跨请求限流形同虚设。
    - 每个请求仍新建一个 DataPlane：候选合并器(CandidateBuilder)与窗口缓冲都是
      有状态累积器，跨请求复用会导致候选无限增长且把不同时间的命中错误合并，
      因此按请求隔离（见 docs/delivery.md 的说明）。DataPlane 通过注入共享
      gateway 获得跨请求限流，同时保持自身的窗口/合并器隔离。
    """

    def __init__(self) -> None:
        self.resolver = None
        self.build_error: str | None = None
        self.request_count = 0
        self.rejected_requests = 0
        # 跨请求共享的接入网关（限流状态所在）
        self.gateway = TelemetryGateway(
            rate_per_sec=settings.rate_per_sec(),
            burst=settings.rate_burst(),
        )
        try:
            self.resolver = build_demo_resolver(_detector_config())
        except Exception as e:            # 缺厂商文档/依赖时不致命，降级为不可用
            self.build_error = f"{type(e).__name__}: {e}"

    def new_plane(self) -> DataPlane | None:
        if self.resolver is None:
            return None
        return DataPlane(self.resolver, gateway=self.gateway)


_state = ServiceState()


# ------------------------------------------------------------------ 工具

def _auth_from_headers(tenant_id: str | None, device_sn: str | None
                       ) -> dict[str, Any]:
    """身份只认请求头（认证主体），不认正文。缺一即拒绝（失败关闭）。"""
    if not tenant_id or not device_sn:
        raise HTTPException(status_code=401,
                            detail="缺少可信身份：需要请求头 X-Tenant-Id 与 X-Device-Sn")
    return {"tenant_id": tenant_id, "device_sn": device_sn, "principal": "http-gateway"}


def _tenant_required(tenant_id: str | None) -> str:
    """读路径的租户上下文：只认 `X-Tenant-Id`，缺失/空 → 401（失败关闭）。

    为什么不接受 `?tenant_id=` 查询参数：查询串可被链接、日志、浏览器历史携带与
    篡改，不是可信身份来源。租户只能来自请求头（认证主体注入的位置）——查询参数
    里的 tenant_id 一律被忽略（有测试断言这一点）。
    """
    if not tenant_id:
        raise HTTPException(status_code=401,
                            detail="缺少可信租户：需要请求头 X-Tenant-Id")
    return tenant_id


def _db_reachable() -> dict[str, Any]:
    """探测数据库连通性：直接建立一条连接（拿到即通，不做任何查询）。"""
    try:
        conn = get_engine().connect()
        conn.close()
        return {"ok": True, "url_scheme": _url_scheme()}
    except Exception as e:                        # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# 探针路径豁免鉴权：编排层/LB 探活时不该被要求带密钥
_PUBLIC_PATHS = frozenset({"/healthz", "/readyz"})

# 分页默认与上限：默认 50，单次最多 500（防一次拉全表）
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 500


def _api_key_ok(provided: str, expected: str) -> bool:
    """常量时间比较，避免按字符逐位试探（时序侧信道）。"""
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _extract_api_key(request) -> str:
    """从请求头取调用方密钥（`X-API-Key`）。"""
    return request.headers.get("X-API-Key", "") or ""


# ------------------------------------------------------------------ 应用工厂

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """启动时跑数据库迁移。连不上不应阻止进程启动——由 /readyz 报未就绪。"""
    setup_logging(version=_VERSION)
    try:
        # 走 alembic 版本化迁移（不再用 create_all —— 它对已存在的表静默跳过，
        # 加列/改类型在生产库上不生效且不报错，是典型的"看起来跑通了"）
        if migrations.auto_migrate_enabled():
            migrations.upgrade_head()
            _log.info("迁移完成", extra={"revision": migrations.current_revision()})
        else:
            # 生产多副本场景：多个实例同时启动会并发跑迁移、互相竞争，
            # 故显式关闭，由部署流程执行 `make migrate`
            _log.info("自动迁移已关闭（DD_AUTO_MIGRATE=0），请确保部署流程已执行 alembic upgrade head")
    except Exception as e:                           # noqa: BLE001
        # 迁移失败不阻止启动：由 /readyz 报未就绪，编排层据此摘流量
        _log.error("迁移失败（服务仍启动，/readyz 将报未就绪）",
                   extra={"error": f"{type(e).__name__}: {e}"})
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="data_dog 遥测故障调查服务",
        version=_VERSION,
        description="P2 在线数据面 HTTP 入口：原始日志 → 候选异常（确定性、失败关闭）。",
        lifespan=_lifespan,
    )

    # -------------------------------------------------- CORS（可选）
    # 仅当 DD_CORS_ORIGINS 显式给出来源时才挂载；未设 = 同源（浏览器默认拦跨源）。
    # 不用 "*"：带凭据的跨源请求下 `*` 会被浏览器拒绝，且会放开所有站点。
    _origins = settings.cors_origins()
    if _origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "X-API-Key", "X-Tenant-Id",
                           "X-Device-Sn", "X-Request-Id", "traceparent"],
            expose_headers=["X-Request-Id", "traceparent"],
        )
        _log.info("CORS 已启用", extra={"origins": _origins})

    # -------------------------------------------------- 统一错误契约

    def _error_body(code: ToolErrorCode, detail: Any = "", *, reason: str = ""
                    ) -> dict[str, Any]:
        """服务边界统一错误体：{ok, error, detail}。错误码保持有限闭集。"""
        body: dict[str, Any] = {"ok": False, "error": code.value, "detail": detail}
        if reason:
            body["reason"] = reason
        return body

    @app.exception_handler(LLMCallError)
    async def _llm_error_handler(request, exc: LLMCallError) -> JSONResponse:
        """LLM 调用失败 → 统一错误码 + 对应 HTTP 状态（超时 504 / 不可用 503）。"""
        return JSONResponse(
            status_code=http_status_for(exc.code),
            content=_error_body(exc.code, exc.detail),
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request, exc: Exception) -> JSONResponse:
        """兜底：未预期异常不泄露内部细节，但仍以统一契约返回。"""
        return JSONResponse(
            status_code=http_status_for(ToolErrorCode.EXECUTION_FAILED),
            content=_error_body(ToolErrorCode.EXECUTION_FAILED,
                                f"{type(exc).__name__}"),
        )

    # -------------------------------------------------- 请求上下文 + 访问日志

    @app.middleware("http")
    async def _request_context(request, call_next):
        """为每个请求建立 request_id + trace 上下文，贯穿该请求内所有日志行。

        - `X-Request-Id`：客户端可透传（与上游对齐），没带则生成；回传响应头。
        - `traceparent`（W3C）：上游带了合法值就沿用其 trace-id；两种情况都新建
          span-id，并回传响应头。见 `core/tracing.py` 的边界说明。
        出事时用 request_id/trace_id 能把一次请求的全部日志捞出来。
        """
        rid = set_request_id(request.headers.get("X-Request-Id"))
        _trace_id, traceparent = start_trace(request.headers.get("traceparent"))
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            clear_request_id()
            clear_trace()
        response.headers["X-Request-Id"] = rid
        response.headers["traceparent"] = traceparent
        # 探针不记访问日志（会被编排层高频轮询，刷屏）
        if request.url.path not in _PUBLIC_PATHS:
            _log.info("请求完成", extra={
                "method": request.method, "path": request.url.path,
                "status": response.status_code, "duration_ms": duration_ms,
                "tenant_id": request.headers.get("X-Tenant-Id", ""),
            })
            # 指标：path 用**路由模板**（控制标签基数）
            metrics.observe_http(request.method, _route_template(request),
                                 response.status_code, duration_ms / 1000.0)
        return response

    # -------------------------------------------------- 鉴权（可选，由 DD_API_KEY 开启）

    @app.middleware("http")
    async def _api_key_guard(request, call_next):
        """设了 DD_API_KEY 才校验：所有非探针路径都必须带正确的 X-API-Key。

        默认不设 → 不校验（本地开发/评测不受影响）。对外暴露时**必须**设置。
        """
        expected = settings.api_key()
        if expected and request.url.path not in _PUBLIC_PATHS:
            if not _api_key_ok(_extract_api_key(request), expected):
                _state.rejected_requests += 1
                return JSONResponse(
                    status_code=401,
                    content=_error_body(ToolErrorCode.DENIED,
                                        "缺少或错误的 X-API-Key",
                                        reason="unauthorized"),
                )
        return await call_next(request)

    # -------------------------------------------------- 探针

    @app.get("/healthz", tags=["probe"])
    def healthz() -> dict[str, Any]:
        """存活探针：进程活着即 200，不触碰任何外部依赖。"""
        return {"status": "ok", "service": settings.service_name(),
                "version": _VERSION, "uptime_s": round(time.time() - _STARTED_AT, 1)}

    @app.get("/readyz", tags=["probe"])
    def readyz() -> JSONResponse:
        """就绪探针：探测数据库连通性。不通 → 503（编排层摘流量，但不重启）。"""
        checks: dict[str, Any] = {"database": _db_reachable()}
        ready = bool(checks["database"]["ok"])

        # 迁移版本（库 schema 是否已到 head）
        try:
            checks["migration"] = {"ok": True,
                                   "revision": migrations.current_revision()}
        except Exception as e:                        # noqa: BLE001
            checks["migration"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

        # 算子绑定（缺厂商文档时降级；只影响接入，不影响服务基本可用）
        if _state.resolver is None:
            checks["resolver"] = {"ok": False, "error": _state.build_error}
        else:
            checks["resolver"] = {"ok": True}

        body = {"status": "ready" if ready else "not_ready", "checks": checks}
        return JSONResponse(status_code=200 if ready else 503, content=body)

    @app.get("/metrics", tags=["probe"])
    def metrics_endpoint() -> Response:
        """Prometheus 文本格式指标（可被 Prometheus 直接抓取）。

        库内行数用 `COUNT(*)` 聚合实时采集（不做全表加载）；采集失败只记
        日志，不让指标端点整体 500。
        """
        # 进程信息（每次抓取时刷新）
        metrics.info.labels(version=_VERSION).set(1)
        metrics.auth_enabled.set(1 if settings.api_key() else 0)
        metrics.resolver_ready.set(1 if _state.resolver is not None else 0)
        metrics.uptime_seconds.set(round(time.time() - _STARTED_AT, 1))
        try:
            with session_scope() as s:
                metrics.db_rows.labels(table="candidates").set(
                    repo.count_rows(s, M.CandidateRow))
                metrics.db_rows.labels(table="alerts").set(
                    repo.count_rows(s, M.AlertRow))
                metrics.db_rows.labels(table="outbox_pending").set(
                    repo.count_rows(s, M.AlertOutboxRow, status="pending"))
        except Exception as e:                        # noqa: BLE001
            _log.warning("采集库内行数失败", extra={
                "error": f"{type(e).__name__}: {e}"})
        return Response(content=metrics.render(),
                        media_type="text/plain; version=0.0.4; charset=utf-8")

    # -------------------------------------------------- 接入

    @app.post("/v1/telemetry", tags=["ingest"])
    def ingest_telemetry(
        payload: dict[str, Any],
        response: Response,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        x_device_sn: str | None = Header(default=None, alias="X-Device-Sn"),
    ) -> dict[str, Any]:
        """接收一批原始日志，跑数据面，产出候选并落库。

        正文格式：
            {"events": [ {原始日志}, ... ]}
        身份来自请求头（X-Tenant-Id / X-Device-Sn），正文里若有同名字段一律被忽略。
        """
        auth = _auth_from_headers(x_tenant_id, x_device_sn)
        events = payload.get("events")
        if not isinstance(events, list) or not events:
            raise HTTPException(status_code=422, detail="正文需含非空 'events' 数组")
        if len(events) > 1000:
            raise HTTPException(status_code=422, detail="单批 events 上限 1000 条")

        plane = _state.new_plane()
        if plane is None:
            _state.rejected_requests += 1
            raise HTTPException(status_code=503,
                                detail=f"算子绑定不可用：{_state.build_error}")

        _state.request_count += 1
        _log.info("接入请求", extra={
            "tenant_id": auth["tenant_id"], "device_sn": auth["device_sn"],
            "events": len(events),
        })
        result = plane.process_batch(events, auth)
        metrics.observe_data_plane(result.stats)
        metrics.candidates_created_total.inc(len(result.candidates))
        _log.info("接入完成", extra={
            "ingested": result.stats.ingested, "resolved": result.stats.resolved,
            "rejected": result.stats.rejected, "candidates": len(result.candidates),
        })

        # 落库（幂等 upsert）：规范化遥测 + 候选
        persisted = {"telemetry": 0, "candidates": 0}
        try:
            with session_scope() as s:
                persisted["telemetry"] = repo.save_telemetry(
                    s, result.normalized, tenant_id=auth["tenant_id"])
                persisted["candidates"] = repo.save_candidates(
                    s, result.candidates, tenant_id=auth["tenant_id"])
        except Exception as e:                        # noqa: BLE001
            # 落库失败不吞掉已算出的结果，但要在响应里标明（不静默降级）
            response.status_code = 207
            return {
                "ok": False, "error": "persist_failed",
                "detail": f"{type(e).__name__}: {e}",
                "stats": _stats_dict(result),
                "candidates": [c.to_dict() for c in result.candidates],
            }

        return {
            "ok": True,
            "stats": _stats_dict(result),
            "persisted": persisted,
            "candidates": [c.to_dict() for c in result.candidates],
        }

    # -------------------------------------------------- 查询

    @app.get("/v1/candidates", tags=["query"])
    def list_candidates(
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        device_sn: str | None = Query(default=None),
        component_ref: str | None = Query(default=None),
        limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            rows = repo.list_candidates(s, tenant_id=tenant, device_sn=device_sn,
                                        component_ref=component_ref,
                                        limit=limit, offset=offset)
            total = repo.count_rows(s, M.CandidateRow, tenant_id=tenant)
        return {"ok": True, "count": len(rows), "total": total,
                "limit": limit, "offset": offset,
                "has_more": offset + len(rows) < total, "items": rows}

    @app.get("/v1/alerts", tags=["query"])
    def list_alerts(
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            rows = repo.list_alerts(s, tenant_id=tenant, limit=limit, offset=offset)
            total = repo.count_rows(s, M.AlertRow, tenant_id=tenant)
        return {"ok": True, "count": len(rows), "total": total,
                "limit": limit, "offset": offset,
                "has_more": offset + len(rows) < total, "items": rows}

    @app.get("/v1/outbox", tags=["query"])
    def list_outbox(
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        status: str | None = Query(default=None),
        limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            rows = repo.list_outbox(s, tenant_id=tenant, status=status,
                                    limit=limit, offset=offset)
            total = repo.count_rows(s, M.AlertOutboxRow, tenant_id=tenant)
        return {"ok": True, "count": len(rows), "total": total,
                "limit": limit, "offset": offset,
                "has_more": offset + len(rows) < total, "items": rows}

    # -------------------------------------------------- 单资源 + 调查

    @app.get("/v1/candidates/{candidate_id}", tags=["query"])
    def get_candidate(
        candidate_id: str,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """取单个候选详情。跨租户（或不存在）→ 404，不泄露存在性。"""
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            item = repo.get_candidate(s, candidate_id, tenant_id=tenant)
        if item is None:
            raise HTTPException(status_code=404, detail="候选不存在")
        return {"ok": True, "item": item}

    @app.get("/v1/candidates/{candidate_id}/evidence", tags=["query"])
    def get_candidate_evidence(
        candidate_id: str,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """Agent 所见证据：候选窗口的遥测 + 参照 + 候选自带证据 + 厂商知识。

        证据由 `RepoEvidenceBridge` 从**同一批落库遥测**重建（与调查时同源），
        因此前端"先看证据、再点调查"不会看到两套对不上的数字。
        """
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            cand = repo.get_candidate(s, candidate_id, tenant_id=tenant)
            if cand is None:
                raise HTTPException(status_code=404, detail="候选不存在")
            rows = repo.list_telemetry(s, tenant_id=tenant,
                                       device_sn=cand["device_sn"])

        bridge = RepoEvidenceBridge(rows, device_sn=cand["device_sn"])
        tel = bridge.query_telemetry(
            cand["device_sn"], cand["component_ref"],
            cand["window_start"], cand["window_end"])
        tel = tel if isinstance(tel, dict) and tel.get("ok") is not False else {}
        ctype = (cand["component_ref"].split(".", 1) or [""])[0]
        vendor = bridge.query_vendor_knowledge(ctype)

        return {
            "ok": True,
            "candidate_id": candidate_id,
            "device_sn": cand["device_sn"],
            "component_ref": cand["component_ref"],
            "window": {"start": cand["window_start"], "end": cand["window_end"]},
            "metrics": tel.get("metrics", {}),
            "reference_window_metrics": tel.get("reference_window_metrics"),
            "evidence": cand.get("evidence", []),
            "vendor_knowledge": vendor,
        }

    @app.get("/v1/reports", tags=["query"])
    def list_reports(
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        """分页列出本租户的历史调查报告（调查结果可复用，不必重跑 LLM）。"""
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            rows = repo.list_reports(s, tenant_id=tenant, limit=limit, offset=offset)
            total = repo.count_rows(s, M.ReportRow, tenant_id=tenant)
        return {"ok": True, "count": len(rows), "total": total,
                "limit": limit, "offset": offset,
                "has_more": offset + len(rows) < total, "items": rows}

    @app.get("/v1/investigations/{investigation_id}", tags=["query"])
    def get_investigation(
        investigation_id: str,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """读已持久化的调查报告（跨租户 → 404）。前端据此免重跑 LLM。"""
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            report = repo.get_report(s, investigation_id, tenant_id=tenant)
        if report is None:
            raise HTTPException(status_code=404, detail="调查报告不存在")
        return {"ok": True, "investigation_id": investigation_id,
                "report": report}

    @app.post("/v1/investigations", tags=["investigate"])
    def create_investigation(
        payload: dict[str, Any],
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """对一个候选跑一次 Agent 调查，产出并持久化报告。

        **同步**实现（FastAPI 线程池）：真实 LLM 时可能耗时 10–60s，
        `DD_INVESTIGATE_TIMEOUT_S` 为**整次调查**的总预算（默认 90s），到点
        失败关闭。诚实边界：同步 + 长超时会占用 worker、进程内有状态，
        **不能水平扩展**——前端须按"长任务"处理（禁用/进度/可取消）。
        本端点**绝不被 /v1/telemetry 调用**：热路径保持无 LLM。
        """
        tenant = _tenant_required(x_tenant_id)
        candidate_id = (payload or {}).get("candidate_id")
        if not candidate_id:
            raise HTTPException(status_code=422, detail="正文需含 'candidate_id'")

        with session_scope() as s:
            row = repo.get_candidate(s, candidate_id, tenant_id=tenant)
            if row is None:
                metrics.investigations_total.labels(result="not_found").inc()
                raise HTTPException(status_code=404, detail="候选不存在")
            tel_rows = repo.list_telemetry(s, tenant_id=tenant,
                                           device_sn=row["device_sn"])

        cand = _row_to_candidate(row)
        trace: list[dict[str, Any]] = []

        try:
            provider = _build_investigation_provider()
        except HTTPException:
            metrics.investigations_total.labels(result="failed").inc()
            raise

        try:
            bridge = RepoEvidenceBridge(tel_rows, device_sn=cand.device_sn)
            tools = InvestigationTools(bridge)
            agent = AgentLoop(provider, tools, model=settings.llm_model() or None,
                              max_steps=settings.investigate_max_steps(),
                              trace_cb=trace.append)
            report = agent.investigate(cand)
        except LLMCallError as e:
            # 超时/不可用 → 统一错误码（504/503），失败关闭，不假装查过证据
            metrics.investigations_total.labels(result="timeout").inc()
            raise HTTPException(status_code=http_status_for(e.code),
                                detail=e.detail) from e
        except Exception as e:                          # noqa: BLE001
            metrics.investigations_total.labels(result="failed").inc()
            raise HTTPException(status_code=500,
                                detail=f"调查执行失败：{type(e).__name__}") from e

        metrics.investigations_total.labels(result="ok").inc()

        # 持久化报告（幂等 upsert）
        try:
            with session_scope() as s:
                repo.save_report(s, report, tenant_id=tenant)
        except Exception as e:                          # noqa: BLE001
            # 落库失败不吞掉已算出的结论，但如实标明
            _log.warning("报告落库失败", extra={"error": f"{type(e).__name__}: {e}"})

        report_dict = report.to_dict()
        usage = report_dict.get("model_provenance", {}).get("usage", {})
        # 人话结论：与演示台/候选详情同源（presentation.derive_verdict）
        tel_map = {cand.component_ref: {
            "metrics": {},
            "device_sn": cand.device_sn,
        }}
        # 从 trace 里取该候选窗口的实测指标，给 verdict 用（与证据端点一致）
        for step in trace:
            res = step.get("result") or {}
            if step.get("name") == "query_candidate_evidence" and res.get("metrics"):
                tel_map[cand.component_ref]["metrics"] = res["metrics"]
                break
        verdict, verdict_detail = derive_verdict(
            [report_dict | {"severity_hint": cand.severity_hint,
                            "component_ref": cand.component_ref,
                            "rule_matches": cand.rule_matches}],
            {}, tel_map)
        return {
            "ok": True,
            "investigation_id": report.investigation_id,
            "candidate_id": candidate_id,
            "report": report_dict,
            "verdict": verdict,
            "verdict_detail": verdict_detail,
            "trace": trace,
            "usage": usage,
        }

    return app


def _url_scheme() -> str:
    url = _database_url()
    return url.split(":", 1)[0] if ":" in url else url


def _stats_dict(result: Any) -> dict[str, Any]:
    st = result.stats
    return {
        "ingested": st.ingested,
        "resolved": st.resolved,
        "rejected": st.rejected,
        "rejected_by_reason": dict(st.rejected_by_reason),
        "normalized": len(result.normalized),
        "candidates": len(result.candidates),
    }


# ---------------------------------------------------------------- 调查端点辅助


def _row_to_candidate(row: dict[str, Any]) -> CandidateEvent:
    """把落库的候选行重建为 `CandidateEvent`（Agent 调查的输入）。"""
    evidence = []
    for e in row.get("evidence", []) or []:
        if isinstance(e, dict):
            evidence.append(EvidenceSummary(
                key=e.get("key", ""),
                from_value=e.get("from_value"),
                to_value=e.get("to_value"),
                baseline=e.get("baseline"),
                unit=e.get("unit"),
                note=e.get("note"),
            ))
        else:
            evidence.append(e)
    return CandidateEvent(
        candidate_id=row["candidate_id"],
        device_sn=row["device_sn"],
        component_ref=row["component_ref"],
        signal=row["signal"],
        severity_hint=row["severity_hint"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        sample_count=int(row.get("sample_count", 0)),
        rule_matches=list(row.get("rule_matches", [])),
        anomaly_score=float(row.get("anomaly_score", 0.0)),
        evidence=evidence,
        evidence_refs=list(row.get("evidence_refs", [])),
        detector_provenance=row.get("detector_provenance", ""),
    )


class _DeadlineProvider:
    """给 provider 加一个**整次调查的总墙钟预算**。

    单次 LLM 调用超时（`Provider.timeout`）只保证"一次调用不会永久挂起"；但一次
    调查最多 `max_steps` 次调用，若每次都快到超时、叠加起来仍可能远超契约承诺的
    90s。本包装器把"整次调查"纳入同一个预算，到点后任何后续调用立即以
    `LLMCallError(TIMEOUT)` 失败关闭——端点因此有确定上界，不会长期占用 worker。
    `AgentLoop` 捕获该异常后会产出 fail-closed 报告（不假装查过证据）。
    """

    def __init__(self, inner: Any, budget_s: float):
        self._inner = inner
        self._deadline = time.monotonic() + max(0.0, budget_s)
        self._budget_s = budget_s

    def chat(self, messages, **kw):
        if time.monotonic() >= self._deadline:
            raise LLMCallError(ToolErrorCode.TIMEOUT,
                               f"调查总预算 {self._budget_s:.0f}s 已耗尽")
        return self._inner.chat(messages, **kw)


def _build_investigation_provider() -> Any:
    """按配置构造调查用的 provider（每次调用新建，保证状态隔离）。

    - `DD_INVESTIGATE_OFFLINE=1` → 离线确定性 provider（CI/测试不调真实模型，
      结论仍由证据推导，非写死剧本）。
    - 否则用已配置的供应商 + key；未配置 → 503（不静默降级成假结论）。
    """
    if settings.investigate_offline():
        return _DeadlineProvider(DeterministicEvidenceProvider(),
                                 settings.investigate_timeout_s())

    cfg = load_config()
    key = (cfg.get("api_key") or "").strip()
    if not key:
        raise HTTPException(
            status_code=503,
            detail="未配置 LLM 凭据（先 data-dog init 设 key，"
                   "或设 DD_INVESTIGATE_OFFLINE=1 用离线确定性 provider）")
    from ..providers.client import Provider

    provider = Provider.from_config(
        cfg.get("vendor_id") or settings.llm_vendor(), key,
        timeout=settings.investigate_timeout_s(),
    )
    return _DeadlineProvider(provider, settings.investigate_timeout_s())


app = create_app()
