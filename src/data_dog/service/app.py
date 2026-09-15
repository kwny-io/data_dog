# -*- coding: utf-8 -*-
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
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from ..data_plane.p2_demo import _detector_config
from ..data_plane.pipeline import DataPlane, build_demo_resolver
from ..storage import repository as repo
from ..storage.db import database_url as _database_url
from ..storage.db import get_engine, init_db, session_scope

_VERSION = "0.1.0"
_STARTED_AT = time.time()


# ------------------------------------------------------------------ 服务状态

class ServiceState:
    """进程内状态：构建一次解析器/绑定，复用到每个请求。

    - resolver/绑定是无状态只读的，构建一次即可。
    - 每个请求新建一个 DataPlane：候选合并器(CandidateBuilder)与窗口缓冲都是
      有状态累积器，跨请求复用会导致候选无限增长且把不同时间的命中错误合并，
      因此按请求隔离（见 docs/delivery.md 的说明）。
    """

    def __init__(self) -> None:
        self.resolver = None
        self.build_error: Optional[str] = None
        self.request_count = 0
        self.rejected_requests = 0
        try:
            self.resolver = build_demo_resolver(_detector_config())
        except Exception as e:            # 缺厂商文档/依赖时不致命，降级为不可用
            self.build_error = f"{type(e).__name__}: {e}"

    def new_plane(self) -> Optional[DataPlane]:
        if self.resolver is None:
            return None
        return DataPlane(self.resolver)


_state = ServiceState()


# ------------------------------------------------------------------ 工具

def _auth_from_headers(tenant_id: Optional[str], device_sn: Optional[str]
                       ) -> Dict[str, Any]:
    """身份只认请求头（认证主体），不认正文。缺一即拒绝（失败关闭）。"""
    if not tenant_id or not device_sn:
        raise HTTPException(status_code=401,
                            detail="缺少可信身份：需要请求头 X-Tenant-Id 与 X-Device-Sn")
    return {"tenant_id": tenant_id, "device_sn": device_sn, "principal": "http-gateway"}


def _db_reachable() -> Dict[str, Any]:
    """探测数据库连通性：直接建立一条连接（拿到即通，不做任何查询）。"""
    try:
        conn = get_engine().connect()
        conn.close()
        return {"ok": True, "url_scheme": _url_scheme()}
    except Exception as e:                        # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# 探针路径豁免鉴权：编排层/LB 探活时不该被要求带密钥
_PUBLIC_PATHS = frozenset({"/healthz", "/readyz"})


def _api_key_ok(provided: str, expected: str) -> bool:
    """常量时间比较，避免按字符逐位试探（时序侧信道）。"""
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _extract_api_key(request) -> str:
    """从请求头取调用方密钥（`X-API-Key`）。"""
    return request.headers.get("X-API-Key", "") or ""


# ------------------------------------------------------------------ 应用工厂

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """启动时建表（幂等）。数据库暂时连不上不应阻止进程启动——由 /readyz 报未就绪。"""
    try:
        init_db()
    except Exception as e:                           # noqa: BLE001
        print(f"[startup] 建表失败（服务仍启动，/readyz 将报未就绪）: {e}")
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="data_dog 遥测故障调查服务",
        version=_VERSION,
        description="P2 在线数据面 HTTP 入口：原始日志 → 候选异常（确定性、失败关闭）。",
        lifespan=_lifespan,
    )

    # -------------------------------------------------- 鉴权（可选，由 DD_API_KEY 开启）

    @app.middleware("http")
    async def _api_key_guard(request, call_next):
        """设了 DD_API_KEY 才校验：所有非探针路径都必须带正确的 X-API-Key。

        默认不设 → 不校验（本地开发/评测不受影响）。对外暴露时**必须**设置。
        """
        expected = os.environ.get("DD_API_KEY", "").strip()
        if expected and request.url.path not in _PUBLIC_PATHS:
            if not _api_key_ok(_extract_api_key(request), expected):
                _state.rejected_requests += 1
                return JSONResponse(
                    status_code=401,
                    content={"ok": False, "error": "unauthorized",
                             "detail": "缺少或错误的 X-API-Key"},
                )
        return await call_next(request)

    # -------------------------------------------------- 探针

    @app.get("/healthz", tags=["probe"])
    def healthz() -> Dict[str, Any]:
        """存活探针：进程活着即 200，不触碰任何外部依赖。"""
        return {"status": "ok", "service": os.environ.get("DD_SERVICE_NAME", "data-dog"),
                "version": _VERSION, "uptime_s": round(time.time() - _STARTED_AT, 1)}

    @app.get("/readyz", tags=["probe"])
    def readyz() -> JSONResponse:
        """就绪探针：探测数据库连通性。不通 → 503（编排层摘流量，但不重启）。"""
        checks: Dict[str, Any] = {"database": _db_reachable()}
        ready = bool(checks["database"]["ok"])

        # 算子绑定（缺厂商文档时降级；只影响接入，不影响服务基本可用）
        if _state.resolver is None:
            checks["resolver"] = {"ok": False, "error": _state.build_error}
        else:
            checks["resolver"] = {"ok": True}

        body = {"status": "ready" if ready else "not_ready", "checks": checks}
        return JSONResponse(status_code=200 if ready else 503, content=body)

    @app.get("/metrics", tags=["probe"])
    def metrics() -> Dict[str, Any]:
        """进程内指标（JSON）。真实部署可替换为 Prometheus 文本格式。"""
        out: Dict[str, Any] = {
            "service": os.environ.get("DD_SERVICE_NAME", "data-dog"),
            "version": _VERSION,
            "uptime_s": round(time.time() - _STARTED_AT, 1),
            "requests_total": _state.request_count,
            "requests_rejected_total": _state.rejected_requests,
            "resolver_ready": _state.resolver is not None,
            "auth_enabled": bool(os.environ.get("DD_API_KEY", "").strip()),
        }
        try:
            with session_scope() as s:
                out["candidates_total"] = len(repo.list_candidates(s))
                out["alerts_total"] = len(repo.list_alerts(s))
                out["outbox_pending_total"] = len(repo.list_outbox(s, status="pending"))
        except Exception as e:                        # noqa: BLE001
            out["db_error"] = f"{type(e).__name__}: {e}"
        return out

    # -------------------------------------------------- 接入

    @app.post("/v1/telemetry", tags=["ingest"])
    def ingest_telemetry(
        payload: Dict[str, Any],
        response: Response,
        x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-Id"),
        x_device_sn: Optional[str] = Header(default=None, alias="X-Device-Sn"),
    ) -> Dict[str, Any]:
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
        result = plane.process_batch(events, auth)

        # 落库（幂等 upsert）：规范化遥测 + 候选
        persisted = {"telemetry": 0, "candidates": 0}
        try:
            with session_scope() as s:
                persisted["telemetry"] = repo.save_telemetry(s, result.normalized)
                persisted["candidates"] = repo.save_candidates(s, result.candidates)
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
        device_sn: Optional[str] = Query(default=None),
        component_ref: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        with session_scope() as s:
            rows = repo.list_candidates(s, device_sn=device_sn,
                                        component_ref=component_ref)
        return {"ok": True, "count": len(rows), "items": rows}

    @app.get("/v1/alerts", tags=["query"])
    def list_alerts() -> Dict[str, Any]:
        with session_scope() as s:
            rows = repo.list_alerts(s)
        return {"ok": True, "count": len(rows), "items": rows}

    @app.get("/v1/outbox", tags=["query"])
    def list_outbox(status: Optional[str] = Query(default=None)) -> Dict[str, Any]:
        with session_scope() as s:
            rows = repo.list_outbox(s, status=status)
        return {"ok": True, "count": len(rows), "items": rows}

    return app


def _url_scheme() -> str:
    url = _database_url()
    return url.split(":", 1)[0] if ":" in url else url


def _stats_dict(result: Any) -> Dict[str, Any]:
    st = result.stats
    return {
        "ingested": st.ingested,
        "resolved": st.resolved,
        "rejected": st.rejected,
        "rejected_by_reason": dict(st.rejected_by_reason),
        "normalized": len(result.normalized),
        "candidates": len(result.candidates),
    }


app = create_app()
