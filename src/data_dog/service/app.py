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

模块划分（拆分后）
------------------
- `service/deps.py`   ：统一错误体 + 鉴权（含声明式依赖），不 import app，避免循环。
- `service/runtime.py`：进程内共享状态与助手（流/作业/检查点/升级/调查执行）。
- `service/routes/`   ：按域的 APIRouter（11 个域、42 个端点）。
- `service/app.py`    ：只剩装配——中间件/异常契约 + `create_app` + `app`。
"""
from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import settings
from ..core import metrics
from ..core.logging import clear_request_id, set_request_id, setup_logging
from ..core.tracing import clear_trace, start_trace
from ..errors import ToolErrorCode, http_status_for
from ..providers.client import LLMCallError
from ..state import get_state_store
from ..storage import migrations
from ..storage.db import reset_tenant_context, set_tenant_context
from .deps import _api_key_ok, _error_body, _extract_api_key
from .partition_maintenance import ensure_if_enabled as _ensure_partitions_if_enabled
from .routes import all_routers
from .runtime import (
    _PUBLIC_PATHS,
    _VERSION,
    ServiceState,  # noqa: F401
    _log,
    _route_template,
    _set_migration_outcome,
    _set_stream,
    _start_checkpoint_if_enabled,
    _start_escalation_if_enabled,
    _start_job_runner_if_needed,
    _startup_mark,
    _state,
    _stop_checkpoint,
    _stop_escalation,
    _stop_job_runner,
    _stream_handle,
    reset_ingest_plane,
)
from .write_behind import start_writer_if_enabled, stop_writer


async def _lifespan(app: FastAPI):
    """启动时跑数据库迁移。连不上不应阻止进程启动——由 /readyz 报未就绪。"""
    setup_logging(version=_VERSION)
    _warn_if_shared_state_unavailable()
    try:
        # 走 alembic 版本化迁移（不再用 create_all —— 它对已存在的表静默跳过，
        # 加列/改类型在生产库上不生效且不报错，是典型的"看起来跑通了"）
        if migrations.auto_migrate_enabled():
            migrations.upgrade_head()
            rev = migrations.current_revision()
            _set_migration_outcome(revision=rev)
            _log.info("迁移完成", extra={"revision": rev})
        else:
            # 生产多副本场景：多个实例同时启动会并发跑迁移、互相竞争，
            # 故显式关闭，由部署流程执行 `make migrate`
            _log.info("自动迁移已关闭（DD_AUTO_MIGRATE=0），请确保部署流程已执行 alembic upgrade head")
    except Exception as e:                           # noqa: BLE001
        # 迁移失败不阻止启动：由 /readyz 报未就绪，编排层据此摘流量
        detail = f"{type(e).__name__}: {e}"
        _set_migration_outcome(error=detail)
        _log.error("迁移失败（服务仍启动，/readyz 将报未就绪）",
                   extra={"error": detail})
        # **关键**：这条不能只进 logging。实测（compose 起旧卷库）迁移失败时，
        # `_log.error` 被调用了但记录到不了 `docker logs`（lifespan 期间根 handler
        # 的 stream 被换过）。启动失败的“为什么”必须无条件可见，否则现场只看到
        # 其它告警刷屏。同时给一条可直接照做的下一步。
        _startup_mark(
            "[data-dog] 启动：数据库迁移失败，服务将以未就绪（/readyz 503）运行。\n"
            f"[data-dog] 原因：{detail.splitlines()[0]}\n"
            "[data-dog] 若报表已存在（DuplicateTable）而库里有旧表却没版本表，说明这是"
            "“迁移前就存在的库”：先 `alembic stamp <与旧表匹配的 revision>`，再 `alembic upgrade head`；"
            "或者在可丢弃的库上 `docker compose down -v && docker compose up -d`。"
        )
    try:
        # A9：分区有效期。默认关（结构变更由集群 CronJob 做）；开了就先补齐未来 N 个月。
        # 失败不阻止启动，但必须记 error——跨月写入会直接报 no partition of relation。
        _ensure_partitions_if_enabled()
    except Exception as e:                           # noqa: BLE001
        _log.error("分区有效期补齐失败（跨月写入可能报 no partition of relation）",
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
        # 但异步提交（默认开；DD_INVESTIGATE_ASYNC=0 退回同步）会 503——故记 error 级。
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
    try:
        start_writer_if_enabled()
    except Exception as e:                           # noqa: BLE001
        # write-behind 是可选增强：起不来必须让该路径**失败关闭**（路由 503），
        # 不能悄悄退回同步——否则 DD_WRITE_BEHIND=1 的语义承诺就落空了。
        _log.error("write-behind 后台写入线程启动失败",
                   extra={"error": f"{type(e).__name__}: {e}"})
    yield
    stop_writer()
    _stop_escalation()
    _stop_job_runner()
    _stop_checkpoint()
    _stop_stream()
def _start_stream_if_enabled() -> None:
    """按 `DD_STREAM_ENABLED` 起流式消费者（长驻 plane + 共享候选构建器）。"""
    if not settings.stream_enabled():
        return
    from ..stream import StreamRunner, build_router, build_source
    plane = _state.new_plane()
    if plane is None:
        raise RuntimeError(f"算子绑定不可用，无法启动流式接入：{_state.build_error}")
    source = build_source()
    router = build_router(source)
    runner = StreamRunner(plane, source, default_tenant=settings.stream_tenant(),
                          router=router)
    thread, stop = runner.start_background()
    _set_stream(runner, source, thread, stop)
    try:
        from ..core import metrics
        metrics.shard_enabled.set(1 if (router and router.enabled) else 0)
    except Exception:                                # noqa: BLE001
        pass
    _log.info("流式接入已启动", extra={
        "source": source.name, "thread_name": thread.name,
        "sharding": bool(router and router.enabled)})
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
    # 运维面访问留痕（低量、高价值）：`/v1/state/*` 能被跨租户读状态快照，
    # "谁在什么时候动过运维面"必须可查（`docs/slo.md` §6 的审计要求）。
    if request.url.path.startswith("/v1/state/") and response.status_code < 400:
        from .. import audit
        audit.record(action=audit.ACTION_OPS_ACCESS, outcome=audit.OUTCOME_ALLOWED,
                     tenant_id=request.headers.get("X-Tenant-Id") or None,
                     actor=audit.key_fingerprint(request.headers.get("X-Ops-Token", "")),
                     target=request.url.path, request_id=rid, trace_id=_trace_id)
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
def _deny(request, *, status: int, code: ToolErrorCode, detail: str,
          reason: str, key_id: str = "") -> JSONResponse:
    """统一拒绝出口：计数 → **留审计** → 返回统一错误体。

    为什么拒绝一定要留痕：`auth.denied` 里最值得看的就是"**声称某租户但密钥不对**"
    这类冒充尝试——它只会出现在失败里。审计写失败不影响这次拒绝的返回
    （`audit.record` 不抛异常），但会记 error 日志 + `data_dog_audit_write_failed_total`。

    `key_id`（②）：密钥环里那把钥匙的**名字**（如 `2026-07`）。它让"**已吊销的那把
    被谁在什么时候用过**"在台账里可查——只有指纹的话，吊销这把与吊销那把分不出来。
    未配密钥环（v1 单密钥）时为空串，审计退回按指纹标识（与 ① 行为一致）。

    注意：守卫是**最外层**中间件（见 `create_app` 的注册顺序说明），此时请求上下文
    还没建立，故 `request_id` 只能取客户端透传的 `X-Request-Id`（可能为空）。
    """
    from .. import audit
    _state.rejected_requests += 1
    audit.record(
        action=audit.ACTION_AUTH_DENIED, outcome=audit.OUTCOME_DENIED,
        tenant_id=request.headers.get("X-Tenant-Id") or None,
        actor=audit.key_fingerprint(_extract_api_key(request)) or "anonymous",
        target=request.url.path, reason=reason, key_id=key_id,
        request_id=request.headers.get("X-Request-Id", ""))
    return JSONResponse(status_code=status,
                        content=_error_body(code, detail, reason=reason))


# 密钥环判定 → (HTTP 状态, reason, 给调用方的话)。
# `unknown_tenant` 与 `mismatch` **必须同码同 reason**：否则"未知租户"与"密钥不对"
# 的差异就成了**租户枚举**接口（拿一把密钥把租户名试过去）。
# `revoked`/`expired` 可以单独回：能走到这一步说明调用方**已经出示了那把密钥本身**，
# 它本来就知道这把钥匙存在——告诉它"这把已失效"是运维必需，不是泄露。
_TENANT_KEY_DENY: dict[str, tuple[int, str, str]] = {
    "mismatch": (403, "tenant_key_mismatch", "密钥与所声称的租户不匹配"),
    "unknown_tenant": (403, "tenant_key_mismatch", "密钥与所声称的租户不匹配"),
    "revoked": (403, "key_revoked", "该密钥已被吊销（轮换后旧钥匙应立即失效）"),
    "expired": (403, "key_expired", "该密钥已过期"),
}


async def _api_key_guard(request, call_next):
    """鉴权守卫：**租户密钥环模式**（设了 `DD_TENANT_KEYS`）优先，否则用全局单密钥。

    两种模式
    --------
    1. **租户密钥环模式**（推荐对外部署用）：请求带 `X-Tenant-Id` 时，`X-API-Key` 必须
       命中该租户密钥环里**处于 active** 的一把（未吊销、未过期）。于是"一把密钥冒充
       所有租户"不再成立：持 A 的密钥去声称 B → 403（未知租户与密钥不符**同码同 reason**，
       不做租户枚举）。不带租户头的路径（`/metrics`、`/v1/state/*` 运维面）仍走全局密钥，
       与既有行为一致。
    2. **全局单密钥模式**（默认，未配 `DD_TENANT_KEYS`）：所有非探针路径都要带正确的
       `X-API-Key`；**不设** `DD_API_KEY` = 不校验（本地开发/评测）。

    ② 的增量：**轮换与吊销**。一个租户可以在册**多把**密钥（v2 写法的 `{"keys": [...]}`）：
    轮换时旧新并存（调用方逐个切、无需同时改），旧的那把置 `revoked_at` 后**下一次请求
    即失效**（`settings` 调用时读取、不缓存，所以不需要重启）。过期（`not_after`）同理。
    被吊销/过期的请求**照样留审计并带上 `key_id`**——"哪把钥匙什么时候被谁用"可查。

    为什么保留模式 2：向后兼容——既有部署、评测、CI 都按单密钥跑，切换不能是"静默改变
    语义"。为什么必须有模式 1：模式 2 下租户身份完全由调用方自报，鉴权只回答"你是不是
    某个合法调用方"，回答不了"你是哪个租户"。配错 `DD_TENANT_KEYS`（JSON 坏 / 环内
    id 重复 / 旧新密钥同值）→ 503 失败关闭，**绝不退回模式 2**。
    """
    if request.url.path in _PUBLIC_PATHS:
        return await call_next(request)

    try:
        keyring = settings.tenant_keyring()
    except ValueError as e:                    # 配置错 → 失败关闭，不降级为单密钥
        return _deny(request, status=503, code=ToolErrorCode.UNAVAILABLE,
                     detail=str(e), reason="tenant_keys_misconfigured")

    provided = _extract_api_key(request)
    if keyring:
        tenant = request.headers.get("X-Tenant-Id", "")
        if tenant:
            key, verdict = settings.resolve_tenant_key(tenant, provided,
                                                       keyring=keyring)
            if verdict != "active":
                status, reason, detail = _TENANT_KEY_DENY[verdict]
                return _deny(request, status=status, code=ToolErrorCode.DENIED,
                             detail=detail, reason=reason,
                             key_id=key.key_id if key is not None else "")
            return await call_next(request)
        # 无租户头 → 非租户作用域，仍用全局密钥兜（未设全局密钥则拒绝）
        expected = settings.api_key()
        if not expected or not _api_key_ok(provided, expected):
            return _deny(request, status=401, code=ToolErrorCode.DENIED,
                         detail="非租户作用域端点需要 X-API-Key",
                         reason="unauthorized")
        return await call_next(request)

    expected = settings.api_key()
    if expected and not _api_key_ok(provided, expected):
        return _deny(request, status=401, code=ToolErrorCode.DENIED,
                     detail="缺少或错误的 X-API-Key", reason="unauthorized")
    return await call_next(request)


async def _llm_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """LLM 调用失败 → 统一错误码 + 对应 HTTP 状态（超时 504 / 不可用 503）。

    参数按 Starlette 异常处理器契约标成 `Exception`（注册的键才是具体类型），
    故这里显式窄化；不是 LLMCallError 就按兜底码处理，绝不猜。
    """
    if isinstance(exc, LLMCallError):
        code, detail = exc.code, exc.detail
    else:                                  # 理论上到不了：本处理器只注册给 LLMCallError
        code, detail = ToolErrorCode.EXECUTION_FAILED, type(exc).__name__
    return JSONResponse(status_code=http_status_for(code), content=_error_body(code, detail))


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底：未预期异常不泄露内部细节，但仍以统一契约返回。"""
    return JSONResponse(
        status_code=http_status_for(ToolErrorCode.EXECUTION_FAILED),
        content=_error_body(ToolErrorCode.EXECUTION_FAILED, f"{type(exc).__name__}"),
    )


def create_app() -> FastAPI:
    """装配 HTTP 服务：CORS + 统一错误契约 + 请求上下文/鉴权中间件 + 各域路由。

    端点实现按域放在 `service/routes/`，进程内共享状态在 `service/runtime.py`，
    鉴权与统一错误体在 `service/deps.py`——本函数只做装配（原先这里内联了 42 个
    端点、1187 行，改一个端点要在这个巨型函数里翻半天）。
    """
    app = FastAPI(
        title="data_dog 遥测故障调查服务",
        version=_VERSION,
        description="P2 在线数据面 HTTP 入口：原始日志 → 候选异常（确定性、失败关闭）。",
        lifespan=_lifespan,
    )

    # 摄入数据面是**一次服务生命周期内唯一**的（见 `runtime.get_ingest_plane`）。
    # 每次装配都清空，否则上一个 app 攒的窗口和限流令牌会漏进来。
    reset_ingest_plane()

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

    # -------------------------------------------------- 异常契约 + 中间件
    # 与 `@app.exception_handler` / `@app.middleware` 装饰器写法等价，但处理函数留在
    # 模块级：否则它们的正文（请求上下文约 35 行）会把 create_app 又撑成巨型函数。
    app.add_exception_handler(LLMCallError, _llm_error_handler)
    app.add_exception_handler(Exception, _unhandled_handler)

    # ⚠ 注册顺序即执行顺序：Starlette 的 add_middleware 是**后加在外层**，
    # 故这里必须保持原顺序（请求上下文 → 鉴权），否则 401 会先于上下文建立返回、
    # 丢掉 X-Request-Id/访问日志与耗时候观测。
    app.middleware("http")(_request_context)
    app.middleware("http")(_api_key_guard)

    # -------------------------------------------------- 路由（按域装配）
    for router in all_routers():
        app.include_router(router)
    return app


app = create_app()
