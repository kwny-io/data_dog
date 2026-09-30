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
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import settings
from ..agent.loop import AgentLoop
from ..alerting.lifecycle import TRANSITIONS
from ..alerting.saas import SaaSTestAdapter
from ..core import metrics
from ..core.logging import (
    clear_request_id,
    get_logger,
    set_request_id,
    setup_logging,
)
from ..core.tracing import clear_trace, start_trace
from ..data_plane.candidate_shared import SharedCandidateBuilder
from ..data_plane.gateway import TelemetryGateway
from ..data_plane.p2_demo import _detector_config
from ..data_plane.pipeline import DataPlane, build_demo_resolver
from ..errors import ToolErrorCode, http_status_for
from ..eval_feedback import summarize_feedback
from ..evidence.refs import EvidenceResolver, make_candidate_metricwin_source
from ..knowledge_ingest import (
    chunk_text,
    make_document_id,
    rank_chunks,
    sha256_text,
)
from ..operator_factory import registry
from ..presentation import derive_verdict
from ..providers.client import LLMCallError
from ..providers.config import load_config
from ..providers.offline_demo import DeterministicEvidenceProvider
from ..schemas.events import CandidateEvent, EvidenceSummary
from ..state import get_state_store
from ..state.checkpoint import diff_checkpoint, write_checkpoint
from ..storage import migrations
from ..storage import models as M
from ..storage import repository as repo
from ..storage.db import database_url as _database_url
from ..storage.db import get_engine, reset_tenant_context, session_scope, set_tenant_context
from ..tools.investigation import InvestigationTools
from .alerting import notify_alert, report_alert
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
        # P3 上报的投递目标（本地测试 SaaS）。进程级共享：其幂等去重表
        # (`_seen`) 跨请求有效，重复投递同一 (key, revision) 只产生一次外部
        # 效果。事件/outbox 的**持久真相**在 DB，这里只是投递端。
        self.saas_adapter = SaaSTestAdapter()
        # 水位线（G2）：开启时**进程级共享**——流式长驻 plane 让所有消息推进同一条
        # 水位线，而不是每请求/每 flush 重置。关闭时为 None（旧行为，零回归）。
        self.watermark: Any = None
        if settings.watermark_enabled():
            from ..data_plane.watermark import Watermark
            self.watermark = Watermark(
                allowed_lateness_s=settings.allowed_lateness_s())
        try:
            self.resolver = build_demo_resolver(_detector_config())
        except Exception as e:            # 缺厂商文档/依赖时不致命，降级为不可用
            self.build_error = f"{type(e).__name__}: {e}"

    def new_plane(self) -> DataPlane | None:
        if self.resolver is None:
            return None
        # 候选构建器走**共享后端**：跨副本/跨请求一致地归并同一设备的命中。
        # 单副本（memory 后端）行为等价；多副本（PG/Redis）才真正共享。
        # 每次请求新建实例，但分组状态在共享存储里，故跨请求仍能正确合并。
        builder = SharedCandidateBuilder(state_store=get_state_store(),
                                         watermark=self.watermark)
        return DataPlane(self.resolver, gateway=self.gateway, builder=builder,
                         watermark=self.watermark)


_state = ServiceState()

# 进程内流式接入句柄（可选）。lifespan 起、关停时清；测试可读其 stats 断言。
_stream_lock = threading.Lock()
_stream: tuple[Any, Any, threading.Thread, threading.Event] | None = None


def _set_stream(runner, source, thread, stop) -> None:
    global _stream
    with _stream_lock:
        _stream = (runner, source, thread, stop)


def _stream_handle():
    with _stream_lock:
        return _stream if _stream is not None else (None, None, None, None)


def _clear_stream() -> None:
    global _stream
    with _stream_lock:
        _stream = None


# ------------------------------------------------------------------ 异步调查 worker 池（G1）

# 进程内单例。lifespan 起、关停时停；测试可读其 alive/queue_depth 断言。
_job_runner_lock = threading.Lock()
_job_runner: Any = None


def _get_job_runner():
    with _job_runner_lock:
        return _job_runner


def _set_job_runner(runner) -> None:
    global _job_runner
    with _job_runner_lock:
        _job_runner = runner


def _start_job_runner_if_needed() -> None:
    """起异步调查 worker 池。

    只在 `DD_INVESTIGATE_ASYNC=1` 时启动——否则 `POST /v1/investigations` 走同步
    路径，无需 worker。worker 的 run_fn 注入 `_execute_investigation`（本模块内
    唯一的调查实现），故同步/异步两条路径**共用同一段执行逻辑**。
    """
    if not settings.investigate_async():
        return
    from .investigation_jobs import JobRunner, SharedJobQueue

    runner = JobRunner(
        _run_investigation_job,
        workers=settings.investigate_workers(),
        queue=SharedJobQueue(),
        poll_interval_s=settings.investigate_queue_poll_ms() / 1000.0,
    )
    runner.start()
    _set_job_runner(runner)


def _stop_job_runner() -> None:
    runner = _get_job_runner()
    if runner is None:
        return
    runner.stop()
    _set_job_runner(None)


# ------------------------------------------------------------------ 状态快照（G3）

_checkpoint_lock = threading.Lock()
_checkpoint_loop: Any = None


def _get_checkpoint_loop():
    with _checkpoint_lock:
        return _checkpoint_loop


def _set_checkpoint_loop(loop) -> None:
    global _checkpoint_loop
    with _checkpoint_lock:
        _checkpoint_loop = loop


def _start_checkpoint_if_enabled() -> None:
    """起周期状态快照任务（`DD_CHECKPOINT_ENABLED=1`）。

    可选：先尝试 `DD_CHECKPOINT_RESTORE=1` 从最新快照恢复（覆盖=否，只补缺失），
    再起周期导出。状态后端不可用时静默跳过（不影响 HTTP 服务）。
    """
    if not settings.checkpoint_enabled():
        return
    from ..state import get_state_store
    from ..state.checkpoint import CheckpointLoop, restore_latest

    store = get_state_store()
    if settings.checkpoint_restore():
        restore_latest(store, overwrite=False)
    loop = CheckpointLoop(store, interval_s=settings.checkpoint_interval_s(),
                          keep=settings.checkpoint_keep(),
                          prefix=settings.checkpoint_prefix())
    loop.start()
    _set_checkpoint_loop(loop)


def _stop_checkpoint() -> None:
    loop = _get_checkpoint_loop()
    if loop is None:
        return
    loop.stop()
    _set_checkpoint_loop(None)


# ------------------------------------------------------------------ 告警升级 sweeper（P4-3）

# 进程内单例。lifespan 起、关停时停；测试可读其 alive 断言。
_escalation_lock = threading.Lock()
_escalation: Any = None


def _get_escalation():
    with _escalation_lock:
        return _escalation


def _set_escalation(sweeper) -> None:
    global _escalation
    with _escalation_lock:
        _escalation = sweeper


def _start_escalation_if_enabled() -> None:
    """起告警升级 sweeper（`DD_NOTIFY_ENABLED=1`）。

    默认关：不配 `DD_NOTIFY_ENABLED` 时告警流止于 SaaS adapter（落库审计），不主动
    外发、不升级。开启后才按值班表派发并计时升级。
    """
    if not settings.notify_enabled():
        return
    from ..alerting.escalation import EscalationEngine, EscalationSweeper

    sweeper = EscalationSweeper(EscalationEngine())
    sweeper.start()
    _set_escalation(sweeper)
    _log.info("告警升级 sweeper 已启动", extra={
        "interval_s": settings.notify_sweep_interval_s()})


def _stop_escalation() -> None:
    sweeper = _get_escalation()
    if sweeper is None:
        return
    sweeper.stop()
    _set_escalation(None)


def _run_investigation_job(tenant_id: str, candidate_id: str,
                           should_cancel) -> dict[str, Any]:
    """worker 的执行回调：跑一次调查，返回 job 终态结果（不抛异常给 worker）。

    返回 `{"status", "report_id", "error_code"}`：
      - 成功 → succeeded + report_id
      - 取消 → cancelled（由 worker 侧 finish_job 拒绝覆盖，这里如实返回 cancelled）
      - 失败 → failed + 有限错误码
    """
    started = time.monotonic()
    status: str = "failed"
    report_id: str | None = None
    error_code: str | None = "execution_failed"
    try:
        outcome = _execute_investigation(
            tenant_id, candidate_id, should_cancel=should_cancel)
        # 取消：provider 抛 _InvestigationCancelled，_execute_investigation 会
        # 把它翻译成 status="cancelled"（不产出报告）。
        status = outcome.get("status", "failed")
        report_id = outcome.get("report_id")
        error_code = outcome.get("error_code")
    except Exception as e:                            # noqa: BLE001
        _log.error("异步调查执行异常", extra={
            "candidate_id": candidate_id,
            "error": f"{type(e).__name__}: {e}"})
        error_code = "execution_failed"
    finally:
        metrics.investigation_job_duration_seconds.observe(time.monotonic() - started)
        metrics.investigation_jobs_total.labels(status=status).inc()
    return {"status": status, "report_id": report_id, "error_code": error_code}


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
    _warn_if_shared_state_unavailable()
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
    try:
        _start_stream_if_enabled()
    except Exception as e:                           # noqa: BLE001
        # 流式接入是可选增强：起不来不该拖垮 HTTP 服务（本身仍可用）
        _log.error("流式接入启动失败（HTTP 入口不受影响）",
                   extra={"error": f"{type(e).__name__}: {e}"})
    try:
        _start_job_runner_if_needed()
    except Exception as e:                           # noqa: BLE001
        # 异步调查 worker 池起不来：不影响已启动的 HTTP 服务（同步路径仍可用），
        # 但配了 DD_INVESTIGATE_ASYNC 的提交会 503——故记 error 级。
        _log.error("异步调查 worker 池启动失败",
                   extra={"error": f"{type(e).__name__}: {e}"})
    try:
        _start_checkpoint_if_enabled()
    except Exception as e:                           # noqa: BLE001
        # 状态快照是可选增强：起不来不影响服务（只是重启时无法从快照恢复）
        _log.error("状态快照任务启动失败",
                   extra={"error": f"{type(e).__name__}: {e}"})
    try:
        _start_escalation_if_enabled()
    except Exception as e:                           # noqa: BLE001
        # 升级 sweeper 是可选增强：起不来不影响 HTTP 服务（只是无人自动升级告警）
        _log.error("告警升级 sweeper 启动失败",
                   extra={"error": f"{type(e).__name__}: {e}"})
    yield
    _stop_escalation()
    _stop_job_runner()
    _stop_checkpoint()
    _stop_stream()


def _start_stream_if_enabled() -> None:
    """按 `DD_STREAM_ENABLED` 起流式消费者（长驻 plane + 共享候选构建器）。"""
    if not settings.stream_enabled():
        return
    from ..stream import StreamRunner, build_source
    plane = _state.new_plane()
    if plane is None:
        raise RuntimeError(f"算子绑定不可用，无法启动流式接入：{_state.build_error}")
    source = build_source()
    runner = StreamRunner(plane, source, default_tenant=settings.stream_tenant())
    thread, stop = runner.start_background()
    _set_stream(runner, source, thread, stop)
    _log.info("流式接入已启动", extra={
        "source": source.name, "thread": thread.name})


def _stop_stream() -> None:
    runner, source, _thread, stop = _stream_handle()
    if runner is None:
        return
    stop.set()
    try:
        source.close()                               # 唤醒 poll，加快线程退出
    except Exception:                                # noqa: BLE001
        pass
    _log.info("流式接入已停止", extra={"stats": runner.stats})


def _warn_if_shared_state_unavailable() -> None:
    """多副本 + memory 状态后端 = 限流/候选合并在各副本各算各的（静默错误）。

    显式告警：不是崩溃（单副本场景 memory 完全正确），但要在日志里说清楚，
    别让运维以为"多副本天然一致"。
    """
    from ..state.memory import InMemoryStateStore
    try:
        store = get_state_store()
    except Exception:                     # noqa: BLE001  后端构建失败由 /readyz 报
        return
    if isinstance(store, InMemoryStateStore):
        _log.warning(
            "共享状态后端为 memory（进程内）：多副本部署下限流与候选合并会各算各的。"
            "多副本请配 DD_STATE_BACKEND=postgres（或 redis）。单副本可忽略。",
            extra={"migration": "state_backend_memory"})


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
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
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
        # 租户上下文：让本请求内所有 `session_scope()` 自动带上 RLS 租户
        # （PostgreSQL 行级安全策略据此放行）。身份来自请求头（认证主体），
        # 与 `_tenant_required` 同源；未带则 None → RLS 下零行可见（失败关闭）。
        tenant_token = set_tenant_context(request.headers.get("X-Tenant-Id"))
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            reset_tenant_context(tenant_token)
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

        # 流式接入（仅在启用时检查）：启用了但消费线程已死 = 数据静默停收，
        # 必须报未就绪——否则"看起来在跑、其实没在收"是最危险的静默故障。
        if settings.stream_enabled():
            runner, source, thread, _stop = _stream_handle()
            if runner is None:
                checks["stream"] = {"ok": False, "error": "已启用但未启动"}
            elif not thread.is_alive():
                checks["stream"] = {"ok": False, "error": "消费线程已退出"}
            else:
                checks["stream"] = {"ok": True, "source": source.name,
                                    "coordination": source.coordination(),
                                    "stats": runner.stats}
                # file 源：展示租约归属（一眼看出哪个副本在读）
                holder = getattr(source, "lease_holder", None)
                if holder is not None:
                    checks["stream"]["lease_holder"] = holder
            ready = ready and bool(checks["stream"]["ok"])

        # 异步调查 worker 池（仅在启用时检查）：配了异步但池子没跑 = 提交会 503，
        # 报未就绪，让编排层把该实例摘出（实例接请求也只会拒）。
        if settings.investigate_async():
            runner = _get_job_runner()
            if runner is None or not runner.alive:
                checks["investigation_workers"] = {"ok": False,
                                                   "error": "异步调查 worker 池未运行"}
            else:
                checks["investigation_workers"] = {"ok": True,
                                                   "queue_depth": runner.queue_depth()}
            ready = ready and bool(checks["investigation_workers"]["ok"])

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
        _runner, _src, _thr, _stop = _stream_handle()
        metrics.stream_up.set(1 if (_thr is not None and _thr.is_alive()) else 0)
        # 异步调查：队列深度（共享队列里的待办数）+ 各状态 job 数
        jr = _get_job_runner()
        if jr is not None:
            depth = jr.queue_depth()
            if depth >= 0:
                metrics.investigation_queue_depth.set(depth)
        try:
            with session_scope() as s:
                metrics.db_rows.labels(table="candidates").set(
                    repo.count_rows(s, M.CandidateRow))
                metrics.db_rows.labels(table="alerts").set(
                    repo.count_rows(s, M.AlertRow))
                metrics.db_rows.labels(table="outbox_pending").set(
                    repo.count_rows(s, M.AlertOutboxRow, status="pending"))
                metrics.db_rows.labels(table="investigation_jobs_pending").set(
                    repo.count_rows(s, M.InvestigationJobRow, status="pending"))
                metrics.db_rows.labels(table="investigation_jobs_running").set(
                    repo.count_rows(s, M.InvestigationJobRow, status="running"))
        except Exception as e:                        # noqa: BLE001
            _log.warning("采集库内行数失败", extra={
                "error": f"{type(e).__name__}: {e}"})
        return Response(content=metrics.render(),
                        media_type="text/plain; version=0.0.4; charset=utf-8")

    # -------------------------------------------------- 状态运维（G3，token 门控）

    def _require_ops_token(x_ops_token: str | None) -> None:
        """运维端点鉴权：无 `DD_OPS_TOKEN` → 404（端点不暴露）；有则须匹配。

        状态快照/diff 含各租户算子状态（敏感），故**独立**于 DD_API_KEY 门控——
        即便开了 API 鉴权，运维端点也只对持 ops token 者开放。
        """
        expected = settings.ops_token()
        if not expected:
            raise HTTPException(status_code=404, detail="未启用运维端点")
        if not _api_key_ok(x_ops_token or "", expected):
            raise HTTPException(status_code=401, detail="缺少或错误的 X-Ops-Token")

    @app.post("/v1/state/checkpoint", tags=["ops"])
    def state_checkpoint(
        x_ops_token: str | None = Header(default=None, alias="X-Ops-Token"),
    ) -> dict[str, Any]:
        """手动触发一次状态快照（并执行保留策略）。"""
        _require_ops_token(x_ops_token)
        store = get_state_store()
        meta = write_checkpoint(store, prefix=settings.checkpoint_prefix())
        with session_scope() as s:
            repo.prune_checkpoints(s, keep=settings.checkpoint_keep())
        return {"ok": True, "checkpoint": meta}

    @app.get("/v1/state/checkpoints", tags=["ops"])
    def list_state_checkpoints(
        x_ops_token: str | None = Header(default=None, alias="X-Ops-Token"),
    ) -> dict[str, Any]:
        """列出快照元数据（不含 payload——不一次拉出所有状态词典）。"""
        _require_ops_token(x_ops_token)
        with session_scope() as s:
            items = repo.list_checkpoints(s, limit=settings.checkpoint_keep() * 4)
        return {"ok": True, "items": items, "total": len(items)}

    @app.get("/v1/state/diff", tags=["ops"])
    def state_diff(
        checkpoint_id: str,
        x_ops_token: str | None = Header(default=None, alias="X-Ops-Token"),
    ) -> dict[str, Any]:
        """快照 vs 当前状态差异（新增/变更/删除的 key）——验证恢复正确性。"""
        _require_ops_token(x_ops_token)
        return {"ok": True, "diff": diff_checkpoint(get_state_store(), checkpoint_id)}

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

    @app.get("/v1/summary", tags=["query"])
    def summary(
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """仪表盘聚合只读端点（租户作用域）。

        一次给前端首屏所需的总量数字，省掉"拉三个列表再数长度"的往返。所有计数
        都**限定在当前租户**（用 COUNT/GROUP BY 聚合，不拉全表）；`uptime_s` /
        `resolver_ready` / `auth_enabled` 是进程级事实，与租户无关。
        """
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            candidates_total = repo.count_rows(s, M.CandidateRow, tenant_id=tenant)
            alerts_by_state = repo.count_alerts_by_state(s, tenant_id=tenant)
            outbox_pending = repo.count_rows(
                s, M.AlertOutboxRow, tenant_id=tenant, status="pending")
        return {
            "ok": True,
            "uptime_s": round(time.time() - _STARTED_AT, 1),
            "candidates_total": candidates_total,
            "alerts_by_state": alerts_by_state,
            "outbox_pending": outbox_pending,
            "resolver_ready": _state.resolver is not None,
            "auth_enabled": bool(settings.api_key()),
        }

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
            "reference_window_metrics": tel.get("reference_window_metrics") or {},
            "evidence": cand.get("evidence", []),
            "vendor_knowledge": vendor,
        }

    @app.get("/v1/candidates/{candidate_id}/refs", tags=["query"])
    def resolve_candidate_refs(
        candidate_id: str,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """把候选的 evidence_refs（不透明引用）解析成**可读证据**。

        引用形如 `metricwin://<candidate_id>` / `rawlog://<event_id>` 等；此前只是
        不透明字符串，审计方读不回内容。这里用 `EvidenceResolver` 解析，并把
        `metricwin` 源**换成按落库候选反查的真实源**（租户作用域，跨租户查不到 →
        not_found，不泄露存在性）。解析逻辑（语法/权限/完整性/有效期）复用同一实现。
        """
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            cand = repo.get_candidate(s, candidate_id, tenant_id=tenant)
            if cand is None:
                raise HTTPException(status_code=404, detail="候选不存在")

            def _lookup(cid: str) -> dict[str, Any] | None:
                return repo.get_candidate(s, cid, tenant_id=tenant)

            resolver = EvidenceResolver()
            resolver.replace_source(
                "metricwin", make_candidate_metricwin_source(_lookup))
            refs = list(cand.get("evidence_refs", [])) or [
                f"metricwin://{candidate_id}"]
            out = resolver.resolve_all(refs)

        out["candidate_id"] = candidate_id
        return out

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

    # 注意：`/v1/investigations/jobs*` **必须**注册在
    # `/v1/investigations/{investigation_id}` 之前——否则后者会把 "jobs" 当成
    # 一个 investigation_id 吃掉（FastAPI 按注册顺序匹配，先注册的优先）。
    @app.get("/v1/investigations/jobs", tags=["investigate"])
    def list_investigation_jobs(
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        status: str | None = Query(default=None),
        limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        """分页列出本租户的异步调查 job（可按 status 过滤）。"""
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            rows = repo.list_jobs(s, tenant_id=tenant, status=status,
                                  limit=limit, offset=offset)
            total = repo.count_rows(s, M.InvestigationJobRow, tenant_id=tenant)
        return {"ok": True, "count": len(rows), "total": total,
                "limit": limit, "offset": offset,
                "has_more": offset + len(rows) < total,
                "items": [_job_public(r) for r in rows]}

    @app.get("/v1/investigations/jobs/{job_id}", tags=["investigate"])
    def get_investigation_job(
        job_id: str,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """查异步调查 job 的状态（跨租户 → 404）。

        status ∈ pending/running/succeeded/failed/cancelled。succeeded 时 `report_id`
        可拿去 `GET /v1/investigations/{report_id}` 读完整报告。
        """
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            job = repo.get_job(s, job_id, tenant_id=tenant)
        if job is None:
            raise HTTPException(status_code=404, detail="调查任务不存在")
        return {"ok": True, "job": _job_public(job)}

    @app.post("/v1/investigations/jobs/{job_id}/cancel", tags=["investigate"])
    def cancel_investigation_job(
        job_id: str,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """请求取消异步调查 job（跨租户 → 404）。

        **协作式**：pending 的直接取消；running 的置 cancelled，worker 在下一个
        provider 调用边界停止（已发出的 LLM 调用要等它返回），故报告可能已落库——
        以 job 的最终状态为准。已终态的任务原样返回，不改。
        """
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            new_status = repo.cancel_job(s, job_id, tenant_id=tenant)
            job = repo.get_job(s, job_id, tenant_id=tenant)
        if new_status is None or job is None:
            raise HTTPException(status_code=404, detail="调查任务不存在")
        return {"ok": True, "job": _job_public(job)}

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
        response: Response,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        sync: bool = Query(default=False, alias="sync"),
    ) -> dict[str, Any]:
        """对一个候选跑一次 Agent 调查。

        **两种模式**（同一段执行逻辑，见 `_execute_investigation`）：
          - **异步**（默认当 `DD_INVESTIGATE_ASYNC=1`）：落一条 pending job、入共享
            队列，**立即**返回 `202 {job_id, status}`；worker 池后台执行。前端轮询
            `GET /v1/investigations/jobs/{job_id}` 取状态/结果。这是端到端水平扩展
            的关键——HTTP worker 不再被长 LLM 调用占满。
          - **同步**（默认，或 `?sync=1`）：同请求内跑完再返回（旧行为，兼容既有
            前端与测试）。真实 LLM 时 10–60s，故前端须按"长任务"处理。

        `DD_INVESTIGATE_TIMEOUT_S` 为**整次调查**的总墙钟预算（默认 90s），两种模式
        都适用，到点失败关闭。本端点**绝不被 /v1/telemetry 调用**：热路径保持无 LLM。
        """
        tenant = _tenant_required(x_tenant_id)
        candidate_id = (payload or {}).get("candidate_id")
        if not candidate_id:
            raise HTTPException(status_code=422, detail="正文需含 'candidate_id'")

        use_async = settings.investigate_async() and not sync
        if use_async:
            return _submit_async_investigation(tenant, candidate_id, response)

        outcome = _execute_investigation(tenant, candidate_id)
        if not outcome.get("ok"):
            raise HTTPException(status_code=outcome["http_status"],
                                detail=outcome["detail"])
        return outcome["response"]

    # -------------------------------------------------- 反馈（P3 闭环回灌）

    # 反馈类型 → 期望的告警目标状态（对齐 alerting.saas 的三种 kind）。
    # false_positive（误报）→ rejected；repaired（已修）→ closed；no_action → 不动状态。
    # 仅当**当前状态允许一步转到目标状态**时才落地（见 alerting.lifecycle.TRANSITIONS）；
    # 否则如实存档、返回 state_applied=null，不强行造出非法/臆造的中间状态。
    #   例：repaired 只有 confirmed/escalated 能一步到 closed；detected 不行。
    _FEEDBACK_KINDS = {"false_positive", "repaired", "no_action"}
    _FEEDBACK_TARGET = {"false_positive": "rejected", "repaired": "closed"}

    @app.post("/v1/feedback", tags=["alerting"])
    def submit_feedback(
        payload: dict[str, Any],
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """提交人工反馈（误报 / 已修 / 无需处理），并**回灌**到告警状态机。

        回灌规则（确定性；状态转移不合法则**如实拒绝**，不强行改）：
          - `false_positive` → 告警转 `rejected`（rejected 是同 Episode 的活跃态，复发可
            重回 detected）
          - `repaired`       → 告警转 `closed`（**仅当当前状态可一步到 closed**，即
            confirmed/escalated；detected/recovering 无直达边，则只存档不改状态）
          - `no_action`      → 仅存档，不改状态
        反馈始终以记录形式落库（可事后审计），状态变更追加进告警 history。
        """
        tenant = _tenant_required(x_tenant_id)
        body = payload or {}
        event_id = body.get("event_id")
        kind = body.get("kind")
        note = body.get("note", "")
        if not event_id or not kind:
            raise HTTPException(status_code=422, detail="正文需含 'event_id' 与 'kind'")
        if kind not in _FEEDBACK_KINDS:
            raise HTTPException(
                status_code=422,
                detail=f"未知反馈类型 {kind!r}（可用：{sorted(_FEEDBACK_KINDS)}）")

        with session_scope() as s:
            alert = repo.get_alert(s, event_id, tenant_id=tenant)
            if alert is None:
                raise HTTPException(status_code=404, detail="告警不存在")
            repo.save_feedback(s, tenant_id=tenant, event_id=event_id,
                               kind=kind, note=note)
            target = _FEEDBACK_TARGET.get(kind)
            applied: str | None = None
            if target is not None:
                legal = target in TRANSITIONS.get(alert["state"], set())
                if legal:
                    repo.set_alert_state(s, tenant_id=tenant, event_id=event_id,
                                         to_state=target,
                                         reason=f"人工反馈：{kind}")
                    applied = target
            updated = repo.get_alert(s, event_id, tenant_id=tenant)

        return {
            "ok": True,
            "event_id": event_id,
            "kind": kind,
            "state_applied": applied,          # null = 未改状态（no_action 或转移不合法）
            "state_now": updated["state"] if updated else None,
            "alert": updated,
        }

    @app.get("/v1/alerts/{event_id}/feedback", tags=["alerting"])
    def list_alert_feedback(
        event_id: str,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """读某条告警的人工反馈历史（租户内；跨租户 → 404）。"""
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            alert = repo.get_alert(s, event_id, tenant_id=tenant)
            if alert is None:
                raise HTTPException(status_code=404, detail="告警不存在")
            items = repo.list_feedback_for_event(s, tenant_id=tenant,
                                                 event_id=event_id)
        return {"ok": True, "event_id": event_id, "count": len(items), "items": items}

    @app.get("/v1/eval/feedback", tags=["eval"])
    def eval_feedback(
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """把人工反馈当**评估标签**，度量 Agent 判得准不准（租户作用域）。

        此前人工反馈只落库 + 改状态——那是"存档"，不是"回灌"。这里把每条反馈与
        它告警对应的 Agent 裁决（`reports.disposition`）关联，算出一致率 / 误报率 /
        漏报率。分母只含**有标签**的行（人工明确判为误报/已修的）；`no_action`
        语义模糊，计入 unlabeled、不混进分子分母（宁可少算，不硬凑数字）。

        与 `/v1/eval/*` 的离线评测集（evals/）互补：那是**离线回归**，这是**线上
        真实反馈**驱动的度量。两者共同回答"Agent 到底判得准不准"。
        """
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            rows = repo.list_feedback_with_report(s, tenant_id=tenant)
        return {"ok": True, "summary": summarize_feedback(rows), "rows": rows}

    # -------------------------------------------------- 用户厂商文档（A7）

    @app.post("/v1/knowledge/documents", tags=["knowledge"])
    def import_knowledge_document(
        payload: dict[str, Any],
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """导入一份**用户自带**厂商文档（纯文本 → 切片 → 落库，租户隔离）。

        正文：`{"source_name": "...", "text": "..."}`。`text` 是**已抽取的纯文本**；
        PDF/DOCX 解析在上游（本项目不做二进制解析）。

        **诚实标注**：本通道的检索是**词法检索**，与内置 chroma 语料（向量语义
        检索）互补、非替代。用户文档按租户隔离——别人的厂商原件你检索不到。
        """
        tenant = _tenant_required(x_tenant_id)
        body = payload or {}
        text = body.get("text")
        source_name = str(body.get("source_name") or "untitled")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=422, detail="正文需含非空 'text' 字符串")
        if len(text) > 2_000_000:
            raise HTTPException(status_code=422, detail="单份文档上限 200 万字符")

        digest = sha256_text(text)
        document_id = make_document_id(digest)
        chunks = chunk_text(text, document_id=document_id)
        with session_scope() as s:
            repo.save_knowledge_document(
                s, tenant_id=tenant,
                meta={"document_id": document_id, "source_name": source_name,
                      "content_sha256": digest, "char_count": len(text)},
                chunks=[{"chunk_id": c.chunk_id, "ordinal": c.ordinal,
                         "text": c.text} for c in chunks])
        return {"ok": True, "document_id": document_id, "source_name": source_name,
                "chunk_count": len(chunks), "char_count": len(text)}

    @app.get("/v1/knowledge/documents", tags=["knowledge"])
    def list_knowledge_documents(
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """列出本租户已导入的厂商文档。"""
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            items = repo.list_knowledge_documents(s, tenant_id=tenant)
        return {"ok": True, "count": len(items), "items": items}

    @app.get("/v1/knowledge/search", tags=["knowledge"])
    def search_knowledge(
        q: str = Query(..., min_length=1),
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        limit: int = Query(default=5, ge=1, le=50),
        document_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """在本租户**已导入**的厂商文档里做**词法检索**，返回命中的切片。

        这是"用户自带文档"的检索通道：确定性、可离线、零依赖。**不是**向量语义
        检索——同义词/近义表述召回请用内置 chroma 语料通道。两者互补。
        """
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            chunks = repo.list_knowledge_chunks(s, tenant_id=tenant,
                                                document_id=document_id)
            docs = {d["document_id"]: d["source_name"]
                    for d in repo.list_knowledge_documents(s, tenant_id=tenant)}
        hits = rank_chunks(chunks, q)[:limit]
        for h in hits:
            h["source_name"] = docs.get(h["document_id"], "")
        return {"ok": True, "query": q, "count": len(hits), "hits": hits}

    @app.delete("/v1/knowledge/documents/{document_id}", tags=["knowledge"])
    def delete_knowledge_document(
        document_id: str,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """删一份用户导入的厂商文档及其切片（租户内；不存在 → 404）。"""
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            ok = repo.delete_knowledge_document(s, document_id, tenant_id=tenant)
        if not ok:
            raise HTTPException(status_code=404, detail="文档不存在")
        return {"ok": True, "document_id": document_id, "deleted": True}

    # -------------------------------------------------- 变更记录（G7）

    @app.post("/v1/changes/ingest", tags=["changes"])
    def ingest_change(
        payload: dict[str, Any],
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """变更系统（CMDB/发布系统）把"变了什么"推来落库（租户隔离，幂等）。

        正文（`change_id` 与 `target` 必填，其余可选；未识别的键进 `raw`，
        对 Agent 返回时展开）：

            {"change_id": "chg-1", "kind": "firmware", "target": "joint_motor.front_left_hip",
             "applied_at": "2026-09-20T10:00:00Z", "from_version": "1.2.0",
             "to_version": "1.2.1", "actor": "ops", "source": "cmdb", ...}

        与 `/v1/knowledge/*` 同源：身份只认请求头 `X-Tenant-Id`（缺失 → 401），
        正文里的同名字段一律被忽略（防越权）。

        **幂等**：同 (租户, change_id) 重复推送**覆盖更新**（不是重复插入），
        返回 `created=false`；故上游可安心重放。
        """
        tenant = _tenant_required(x_tenant_id)
        body = payload or {}
        change_id = str(body.get("change_id") or "").strip()
        target = str(body.get("target") or "").strip()
        if not change_id:
            raise HTTPException(status_code=422, detail="需含非空 'change_id'")
        if not target:
            raise HTTPException(status_code=422, detail="需含非空 'target'")

        _KNOWN = ("change_id", "kind", "target", "applied_at",
                  "from_version", "to_version", "actor", "source")
        record = {
            "change_id": change_id,
            "kind": str(body.get("kind") or ""),
            "target": target,
            "applied_at": str(body.get("applied_at") or ""),
            "from_version": str(body.get("from_version") or ""),
            "to_version": str(body.get("to_version") or ""),
            "actor": str(body.get("actor") or ""),
            "source": str(body.get("source") or ""),
            # 来源特有字段展开给 Agent（对齐内置表形状）
            "raw": {k: v for k, v in body.items() if k not in _KNOWN},
        }
        with session_scope() as s:
            created = repo.save_change_record(s, tenant_id=tenant, record=record)
        return {"ok": True, "change_id": change_id, "target": target,
                "created": created}

    @app.get("/v1/changes", tags=["changes"])
    def list_changes(
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        target: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        """列出本租户的变更记录（可按 `target` 过滤、分页）。"""
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            items = repo.list_change_records(
                s, tenant_id=tenant, target=target, limit=limit, offset=offset)
        return {"ok": True, "count": len(items), "items": items}

    # -------------------------------------------------- 算子控制面（P4-2）

    def _operator_canary_thresholds():
        from ..operator_factory.registry import CanaryThresholds
        return CanaryThresholds(
            max_crash_rate=settings.canary_max_crash_rate(),
            max_parse_drop=settings.canary_max_parse_drop(),
            max_candidate_multiple=settings.canary_max_candidate_multiple(),
            min_samples=settings.canary_min_samples(),
        )

    def _operator_signing_kwargs() -> dict[str, Any]:
        """验签凭据：配了公钥走 Ed25519，否则走 HMAC 密钥。都空 → 让注册失败关闭。"""
        pk = settings.operator_pubkey()
        if pk:
            return {"pubkey": pk}
        return {"secret": settings.operator_signing_key()}

    @app.post("/v1/operators", tags=["operators"])
    def register_operator(
        payload: dict[str, Any],
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """注册一个算子版本（**验签通过方可**，失败关闭）。

        正文：`{"operator_id","version","dsl_source","signature":{...}}`。
        签名绑定 (operator_id, version, sha256(dsl_source))——改一字节即验签失败。
        未配置签名密钥/公钥 → 503（不静默放行未签名的制品）。
        """
        tenant = _tenant_required(x_tenant_id)
        body = payload or {}
        for k in ("operator_id", "version", "dsl_source", "signature"):
            if not body.get(k):
                raise HTTPException(status_code=422, detail=f"正文需含非空 '{k}'")
        kw = _operator_signing_kwargs()
        if not kw.get("secret") and not kw.get("pubkey"):
            raise HTTPException(
                status_code=503,
                detail="未配置签名凭据（DD_OPERATOR_SIGNING_KEY 或 DD_OPERATOR_PUBKEY）")
        with session_scope() as s:
            try:
                row = registry.register_operator(
                    s, tenant_id=tenant, operator_id=body["operator_id"],
                    version=body["version"], dsl_source=body["dsl_source"],
                    signature=body["signature"], actor="http", **kw)
            except registry.SignatureRejected as e:
                raise HTTPException(status_code=422, detail=f"验签失败：{e}") from e
        return {"ok": True, "operator": row}

    @app.get("/v1/operators", tags=["operators"])
    def list_operators(
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """列出本租户的算子（按 operator_id 聚合，含当前 Active/最新版本）。"""
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            items = repo.list_operators(s, tenant_id=tenant)
        return {"ok": True, "count": len(items), "items": items}

    @app.get("/v1/operators/{operator_id}/{version}", tags=["operators"])
    def get_operator(
        operator_id: str, version: str,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """取一个算子版本详情（含 rollout、指标快照、转移台账）。跨租户 → 404。"""
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            row = repo.get_operator_version(
                s, tenant_id=tenant, operator_id=operator_id, version=version)
            if row is None:
                raise HTTPException(status_code=404, detail="算子版本不存在")
            events = repo.list_rollout_events(
                s, tenant_id=tenant, operator_id=operator_id, version=version)
        return {"ok": True, "operator": row, "rollout_events": events}

    @app.post("/v1/operators/{operator_id}/{version}/transition", tags=["operators"])
    def transition_operator(
        operator_id: str, version: str,
        payload: dict[str, Any] | None = None,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """状态转移（合法性由 lifecycle 状态机判定；非法 → 422）。

        正文可选：`{"to": "Shadow", "reason": "...", "evidence": {...}}`。
        """
        tenant = _tenant_required(x_tenant_id)
        body = payload or {}
        target = body.get("to")
        if not target:
            raise HTTPException(status_code=422, detail="正文需含 'to'（目标状态）")
        with session_scope() as s:
            try:
                row = registry.transition_operator(
                    s, tenant_id=tenant, operator_id=operator_id, version=version,
                    target=target, evidence=body.get("evidence"), actor="http")
            except registry.RegistryError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e
        return {"ok": True, "operator": row}

    @app.post("/v1/operators/{operator_id}/{version}/rollback", tags=["operators"])
    def rollback_operator(
        operator_id: str, version: str,
        payload: dict[str, Any] | None = None,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """回滚一个算子版本（→ RolledBack；非删除）。返回回滚到的上一签名版本。"""
        tenant = _tenant_required(x_tenant_id)
        body = payload or {}
        reason = str(body.get("reason") or "手动回滚")
        with session_scope() as s:
            try:
                row = registry.rollback_operator(
                    s, tenant_id=tenant, operator_id=operator_id, version=version,
                    reason=reason, actor="http")
            except registry.RegistryError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e
            prev = registry.previous_signed_version(
                s, tenant_id=tenant, operator_id=operator_id, version=version)
        return {"ok": True, "operator": row, "fallback_to": prev}

    @app.post("/v1/operators/{operator_id}/{version}/canary", tags=["operators"])
    def start_canary(
        operator_id: str, version: str,
        payload: dict[str, Any],
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """启动/调整灰度：`{"percent": 0-100}`。分流是**确定性**的（同设备稳定落同侧）。"""
        tenant = _tenant_required(x_tenant_id)
        body = payload or {}
        if "percent" not in body:
            raise HTTPException(status_code=422, detail="正文需含 'percent'")
        try:
            percent = int(body["percent"])
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=422, detail="'percent' 须为整数") from e
        with session_scope() as s:
            try:
                row = registry.start_canary(
                    s, tenant_id=tenant, operator_id=operator_id, version=version,
                    percent=percent, actor="http")
            except registry.RegistryError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e
        return {"ok": True, "operator": row}

    @app.post("/v1/operators/{operator_id}/{version}/watchdog", tags=["operators"])
    def operator_watchdog(
        operator_id: str, version: str,
        payload: dict[str, Any],
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """喂入 canary/stable 指标，看门狗越限则**自动回滚**。

        正文：`{"canary": {...}, "stable": {...}}`。样本不足 `DD_CANARY_MIN_SAMPLES`
        时**保守不判**（不自动推进/回滚）——见 P4-2 §6。
        """
        tenant = _tenant_required(x_tenant_id)
        body = payload or {}
        with session_scope() as s:
            try:
                result = registry.evaluate_watchdog(
                    s, tenant_id=tenant, operator_id=operator_id, version=version,
                    canary_metrics=body.get("canary") or {},
                    stable_metrics=body.get("stable") or {},
                    thresholds=_operator_canary_thresholds())
            except registry.RegistryError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
        return {"ok": True, **result}

    # -------------------------------------------------- 退化趋势预测（P4-7）

    @app.get("/v1/devices/{device_sn}/forecast", tags=["forecast"])
    def get_device_forecast(
        device_sn: str,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """该设备各已配阈值信号的退化预测（到达阈值 ETA ± 区间）。

        **按需现算**：从落库遥测重建各信号序列 → Theil–Sen 外推。不给 ETA 的信号
        也回带斜率/置信（`eta_hours is None` = 数据不足/不显著/超视界，失败关闭）。
        """
        from ..data_plane.forecast import forecast_to_threshold, series_from_rows
        tenant = _tenant_required(x_tenant_id)
        thresholds = settings.forecast_thresholds()
        with session_scope() as s:
            rows = repo.list_telemetry(s, tenant_id=tenant, device_sn=device_sn)

        items = []
        for signal, threshold in thresholds.items():
            series = series_from_rows(rows, signal)
            fc = forecast_to_threshold(
                series, threshold, signal=signal,
                min_samples=settings.forecast_min_samples(),
                min_confidence=settings.forecast_min_conf(),
                horizon_hours=settings.forecast_horizon_h())
            metrics.observe_forecast(fc.eta_hours is not None, fc.confidence)
            items.append(fc.to_dict())
        return {"ok": True, "device_sn": device_sn,
                "thresholds": thresholds, "items": items}

    @app.get("/v1/candidates/{candidate_id}/forecast", tags=["forecast"])
    def get_candidate_forecast(
        candidate_id: str,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """该候选所在设备、其信号（候选 signal）的退化预测。

        信号来自候选本身；设备来自候选行——故只对「候选关心的那个信号」算一条。
        候选取自落库遥测（与调查同源），不另存预测。
        """
        from ..data_plane.forecast import (
            forecast_to_threshold,
            metric_of_signal,
            series_from_rows,
        )
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            cand = repo.get_candidate(s, candidate_id, tenant_id=tenant)
            if cand is None:
                raise HTTPException(status_code=404, detail="候选不存在")
            rows = repo.list_telemetry(s, tenant_id=tenant,
                                       device_sn=cand["device_sn"])

        signal = cand["signal"]
        # 候选 signal 是语义族（temperature_high），阈值按原始指标（temperature）配
        metric = metric_of_signal(signal)
        threshold = settings.forecast_thresholds().get(metric)
        if threshold is None:
            return {"ok": True, "candidate_id": candidate_id, "device_sn":
                    cand["device_sn"], "signal": signal, "metric": metric,
                    "forecast": None,
                    "reason": "该信号未配阈值（DD_FORECAST_THRESHOLDS），不预测"}
        series = series_from_rows(rows, metric)
        fc = forecast_to_threshold(
            series, threshold, signal=signal,
            min_samples=settings.forecast_min_samples(),
            min_confidence=settings.forecast_min_conf(),
            horizon_hours=settings.forecast_horizon_h())
        metrics.observe_forecast(fc.eta_hours is not None, fc.confidence)
        return {"ok": True, "candidate_id": candidate_id,
                "device_sn": cand["device_sn"], "signal": signal,
                "metric": metric, "forecast": fc.to_dict()}

    # -------------------------------------------------- 通知 / 值班 / 升级（P4-3）

    @app.post("/v1/alerts/{event_id}/ack", tags=["notify"])
    def ack_alert(
        event_id: str,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """确认告警（停止升级）：该告警所有 pending 投递置 acked。

        幂等：重复 ack 不会二次改动（第二次改动条数为 0）。被确认意味着有人接手，
        若告警状态允许则一并推进到 `recovering`（转移不合法则只 ack，不硬造状态）。
        """
        from ..alerting.escalation import EscalationEngine
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            if repo.get_alert(s, event_id, tenant_id=tenant) is None:
                raise HTTPException(status_code=404, detail="告警不存在")
        engine = EscalationEngine()
        acked = engine.ack(tenant_id=tenant, event_id=event_id)
        return {"ok": True, "event_id": event_id, "acked": acked}

    @app.get("/v1/oncall", tags=["notify"])
    def get_oncall(
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        domain: str | None = None,
        at: float | None = None,
    ) -> dict[str, Any]:
        """当前值班表。`domain` 可选过滤；`at`（epoch 秒）可选指定时刻。

        另给 `resolved`：在 `at`（默认现在）解析出的"当前一线值班人"（无则回退更
        高级别）——这就是告警实际会派给谁。
        """
        from ..alerting.oncall import current_oncall, rows_to_entries
        tenant = _tenant_required(x_tenant_id)
        now = float(at) if at is not None else time.time()
        with session_scope() as s:
            rows = repo.list_rota(s, tenant_id=tenant, domain=domain)
        entries = rows_to_entries(rows)
        resolved = current_oncall(entries, domain=domain, at=now)
        return {"ok": True, "count": len(rows),
                "items": [e.to_dict() for e in entries],
                "resolved": resolved.to_dict() if resolved else None}

    @app.put("/v1/oncall", tags=["notify"])
    def put_oncall(
        payload: dict[str, Any],
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    ) -> dict[str, Any]:
        """设置一条值班（`{domain, level, assignee, start_at, end_at, channel, target}`）。

        `rota_id` 由 (domain, level, start_at) 确定性派生——同一排班反复 PUT 收敛到
        同一行。`start_at`/`end_at` 为 epoch 秒（`end_at` 须晚于 `start_at`）。
        """
        from ..alerting.oncall import make_rota_id
        tenant = _tenant_required(x_tenant_id)
        body = payload or {}
        try:
            domain = str(body.get("domain") or "*")
            level = int(body.get("level", 1))
            assignee = str(body.get("assignee") or "")
            start_at = float(body["start_at"])
            end_at = float(body["end_at"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(
                status_code=422,
                detail="需含 {domain?, level, assignee, start_at, end_at}（时间为 epoch 秒）"
            ) from None
        if level < 1:
            raise HTTPException(status_code=422, detail="level 须 ≥ 1")
        if end_at <= start_at:
            raise HTTPException(status_code=422, detail="end_at 须晚于 start_at")
        channel = str(body.get("channel") or "webhook")
        target = str(body.get("target") or "")
        rota_id = make_rota_id(domain, level, start_at)
        with session_scope() as s:
            row = repo.upsert_rota(
                s, tenant_id=tenant, rota_id=rota_id, domain=domain,
                level=level, assignee=assignee, start_at=start_at,
                end_at=end_at, channel=channel, target=target)
        return {"ok": True, "rota": row}

    @app.get("/v1/notifications", tags=["notify"])
    def list_notifications(
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        event_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """投递记录（审计）：每次通知尝试一行，含状态/值班人/ack 截止。

        可按 `event_id` / `status` 过滤。这是回答"这条告警到底通知到谁、确认了没"
        的落点。
        """
        tenant = _tenant_required(x_tenant_id)
        with session_scope() as s:
            rows = repo.list_deliveries(s, tenant_id=tenant,
                                        event_id=event_id, status=status,
                                        limit=limit)
        return {"ok": True, "count": len(rows), "items": rows}

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


# ---------------------------------------------------------------- 调查执行（同步/异步共用）

def _job_public(job: dict[str, Any]) -> dict[str, Any]:
    """job 行的对外视图（去掉内部 worker_id，保留可轮询的字段）。"""
    return {
        "job_id": job["job_id"],
        "candidate_id": job["candidate_id"],
        "status": job["status"],
        "report_id": job.get("report_id"),
        "error_code": job.get("error_code"),
        "submitted_at": job.get("submitted_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


def _submit_async_investigation(tenant: str, candidate_id: str,
                                response: Response) -> dict[str, Any]:
    """提交异步调查：校验候选存在 → 落 pending job → 入共享队列 → 202 返回句柄。

    **立即返回**（不跑 LLM）：这是把 worker 从长调用里解放出来的关键。
    候选不存在 → 404（与同步路径一致的失败语义）。
    """
    with session_scope() as s:
        row = repo.get_candidate(s, candidate_id, tenant_id=tenant)
    if row is None:
        metrics.investigations_total.labels(result="not_found").inc()
        raise HTTPException(status_code=404, detail="候选不存在")

    runner = _get_job_runner()
    if runner is None or not runner.alive:
        # 配了 DD_INVESTIGATE_ASYNC 但 worker 池没起来：失败关闭，不假装受理。
        raise HTTPException(status_code=503,
                            detail="异步调查 worker 池未运行（检查启动日志）")

    job_id = _make_job_id(tenant, candidate_id)
    with session_scope() as s:
        repo.create_job(s, tenant_id=tenant, job_id=job_id,
                        candidate_id=candidate_id)
    runner.submit(tenant_id=tenant, candidate_id=candidate_id, job_id=job_id)
    metrics.investigation_jobs_total.labels(status="pending").inc()

    response.status_code = 202
    return {"ok": True, "job_id": job_id, "status": "pending",
            "candidate_id": candidate_id,
            "poll": f"/v1/investigations/jobs/{job_id}"}


def _execute_investigation(tenant: str, candidate_id: str, *,
                           should_cancel=None) -> dict[str, Any]:
    """一次调查的**唯一实现**（同步端点与异步 worker 共用）。

    返回统一结构，两条路径各自翻译：
      - 同步端点看 `ok`/`http_status`/`detail`/`response`。
      - worker 看 `status`（succeeded/failed/cancelled）/`report_id`/`error_code`。

    失败/取消**不抛异常**（除取消用哨兵外），全部收敛成返回值——这样 worker 不必
    区分异常种类，同步端点也能稳定映射 HTTP 状态。
    """
    with session_scope() as s:
        row = repo.get_candidate(s, candidate_id, tenant_id=tenant)
        if row is None:
            metrics.investigations_total.labels(result="not_found").inc()
            return {"ok": False, "http_status": 404, "detail": "候选不存在",
                    "status": "failed", "error_code": "not_found"}
        tel_rows = repo.list_telemetry(s, tenant_id=tenant,
                                       device_sn=row["device_sn"])

    cand = _row_to_candidate(row)
    trace: list[dict[str, Any]] = []

    try:
        provider = _build_investigation_provider(should_cancel=should_cancel)
    except HTTPException as e:
        metrics.investigations_total.labels(result="failed").inc()
        return {"ok": False, "http_status": e.status_code, "detail": e.detail,
                "status": "failed", "error_code": "unavailable"}

    try:
        bridge = RepoEvidenceBridge(tel_rows, device_sn=cand.device_sn)
        tools = InvestigationTools(bridge)
        agent = AgentLoop(provider, tools, model=settings.llm_model() or None,
                          max_steps=settings.investigate_max_steps(),
                          trace_cb=trace.append)
        report = agent.investigate(cand)
    except _InvestigationCancelled:
        # 协作式取消：不产出报告（要的就是"停，且不留半成品结论"）
        metrics.investigations_total.labels(result="cancelled").inc()
        return {"ok": False, "http_status": 409, "detail": "调查已被取消",
                "status": "cancelled", "error_code": "cancelled"}
    except LLMCallError as e:
        # 超时/不可用 → 统一错误码（504/503），失败关闭，不假装查过证据
        metrics.investigations_total.labels(result="timeout").inc()
        return {"ok": False, "http_status": http_status_for(e.code), "detail": e.detail,
                "status": "failed", "error_code": e.code.value}
    except Exception as e:                          # noqa: BLE001
        metrics.investigations_total.labels(result="failed").inc()
        return {"ok": False, "http_status": 500,
                "detail": f"调查执行失败：{type(e).__name__}",
                "status": "failed", "error_code": "execution_failed"}

    metrics.investigations_total.labels(result="ok").inc()

    # 持久化报告（幂等 upsert）
    try:
        with session_scope() as s:
            repo.save_report(s, report, tenant_id=tenant)
    except Exception as e:                          # noqa: BLE001
        # 落库失败不吞掉已算出的结论，但如实标明
        _log.warning("报告落库失败", extra={"error": f"{type(e).__name__}: {e}"})

    # ---- P3：策略门 → 正式事件 → outbox（全部落库）----
    # 这一步让 /v1/alerts、/v1/feedback、/v1/summary 的 alerts_by_state 在真实
    # 运行里**有数据**——此前后端从不产告警，这些端点是悬空的。失败不吞掉已
    # 算出的结论，但如实回报（alerting 字段标 reported=False + 原因）。
    alerting_result: dict[str, Any] = {"reported": False, "reason": "未执行"}
    try:
        with session_scope() as s:
            alerting_result = report_alert(
                s, tenant_id=tenant, cand=cand, report=report.to_dict(),
                adapter=_state.saas_adapter)
    except Exception as e:                          # noqa: BLE001
        _log.warning("P3 上报失败", extra={"error": f"{type(e).__name__}: {e}"})
        alerting_result = {"reported": False,
                           "reason": f"{type(e).__name__}: {e}"}

    # ---- P4-3：按值班表派发通知（默认关；无值班人则失败关闭）----
    # 在告警落库事务**提交之后**（独立会话）做，避免与落库事务争同一连接锁。
    if alerting_result.get("reported") and alerting_result.get("new"):
        try:
            alerting_result["dispatch"] = notify_alert(
                tenant, str(alerting_result.get("event_id", "")))
        except Exception as e:                      # noqa: BLE001
            _log.warning("P4-3 通知派发失败",
                         extra={"error": f"{type(e).__name__}: {e}"})
            alerting_result["dispatch"] = {"dispatched": False,
                                           "reason": f"{type(e).__name__}: {e}"}

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
    response = {
        "ok": True,
        "investigation_id": report.investigation_id,
        "candidate_id": candidate_id,
        "report": report_dict,
        "verdict": verdict,
        "verdict_detail": verdict_detail,
        "trace": trace,
        "usage": usage,
        "alerting": alerting_result,
    }
    return {"ok": True, "response": response, "status": "succeeded",
            "report_id": report.investigation_id, "error_code": None}


def _make_job_id(tenant_id: str, candidate_id: str) -> str:
    from .investigation_jobs import make_job_id
    return make_job_id(tenant_id, candidate_id)


class _InvestigationCancelled(Exception):
    """协作式取消信号：worker 在 provider 调用边界发现 job 已被取消。"""


class _DeadlineProvider:
    """给 provider 加一个**整次调查的总墙钟预算**（可选叠加协作式取消）。

    单次 LLM 调用超时（`Provider.timeout`）只保证"一次调用不会永久挂起"；但一次
    调查最多 `max_steps` 次调用，若每次都快到超时、叠加起来仍可能远超契约承诺的
    90s。本包装器把"整次调查"纳入同一个预算，到点后任何后续调用立即以
    `LLMCallError(TIMEOUT)` 失败关闭——端点因此有确定上界，不会长期占用 worker。
    `AgentLoop` 捕获该异常后会产出 fail-closed 报告（不假装查过证据）。

    `should_cancel`（G1 异步取消）：每次调用前检查一次，为真则抛
    `_InvestigationCancelled`——它**不被 AgentLoop 捕获**（与 TIMEOUT 不同），
    故取消时不产出报告。这正是取消想要的："停，且不留半成品结论"。
    """

    def __init__(self, inner: Any, budget_s: float, should_cancel=None):
        self._inner = inner
        self._deadline = time.monotonic() + max(0.0, budget_s)
        self._budget_s = budget_s
        self._should_cancel = should_cancel

    def chat(self, messages, **kw):
        if self._should_cancel is not None and self._should_cancel():
            raise _InvestigationCancelled()
        if time.monotonic() >= self._deadline:
            raise LLMCallError(ToolErrorCode.TIMEOUT,
                               f"调查总预算 {self._budget_s:.0f}s 已耗尽")
        return self._inner.chat(messages, **kw)


def _build_investigation_provider(should_cancel=None) -> Any:
    """按配置构造调查用的 provider（每次调用新建，保证状态隔离）。

    - `DD_INVESTIGATE_OFFLINE=1` → 离线确定性 provider（CI/测试不调真实模型，
      结论仍由证据推导，非写死剧本）。
    - 否则用已配置的供应商 + key；未配置 → 503（不静默降级成假结论）。
    """
    if settings.investigate_offline():
        return _DeadlineProvider(DeterministicEvidenceProvider(),
                                 settings.investigate_timeout_s(),
                                 should_cancel=should_cancel)

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
    return _DeadlineProvider(provider, settings.investigate_timeout_s(),
                             should_cancel=should_cancel)


app = create_app()
