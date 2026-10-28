"""仿真控制台 HTTP 服务：`python -m data_dog.go2_sim.server`。

它和 `data_dog.service.app` 的关系
---------------------------------
**不是一个东西，也不该合并**：
- `service.app` 是 data_dog 的业务 API（候选、调查、知识库…），有认证、DB、
  租户隔离，为生产部署。
- 本服务是**演示用的仿真前端**：只有一台 Go2、几组控制端点、一块页面。
  它把遥测**推进 data_dog**（走真实网关/算子链路），自己不做诊断。

把仿真器塞进业务 API 会把一台 MuJoCo 实体的生命周期绑到 Web 服务上——
多副本、滚动更新、健康检查全都要跟着改。分开是对的。

端点
----
    GET  /                    控制台页面
    GET  /api/state           快照（倍率/实际倍率/姿态/故障/子系统）
    GET  /api/telemetry       最近 N 个采样的关节读数（给 12 关节表）
    GET  /api/frame.jpg       当前画面
    GET  /api/faults          故障库（24 条）+ 姿态列表
    GET  /api/candidates      data_dog 产出的候选（本服务转发的诊断结果）
    POST /api/control         倍率 / 暂停
    POST /api/inject          运行中注入故障（不重启）
    POST /api/reset           重开一局（可换姿态）

运行
----
    python -m data_dog.go2_sim.server --port 8099
"""
from __future__ import annotations

import argparse
import threading
from datetime import datetime, timezone
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from .faults import FAULT_LIBRARY, fault_by_key
from .mqtt_pub import MqttTelemetryPublisher
from .postures import POSTURES
from .rig import SimConfig
from .session import SPEEDS, SimEvent, SimSession

# 演示身份。与 MQTT 侧一致：身份来自信道配置，不来自报文。
DEMO_TENANT = "tenant-go2-demo"
DEMO_DEVICE = "device-go2-sim-01"


class _PlaneSink:
    """把会话产出的事件喂给 data_dog 的在线数据面。

    为什么在**服务进程里**跑数据面而不是发 HTTP 给业务服务：
    演示要的是"零配置起两个进程就能看"，而业务服务还要求 DB 迁移完成。
    数据面本身（Gateway+Detector+CandidateBuilder）是纯内存可跑的，
    这里就地建一份，走的仍是同一条 `process_one` 链路——不是绕过。

    **为什么要两条 DataPlane**（本文件最容易被误改的一处）
    ------------------------------------------------------
    一台设备只能有**一个**绑定。`CompatibilityResolver.resolve()` 只按
    vendor/model/firmware/protocol 匹配、**不看部件**，匹配到 2 个就
    `ambiguous` 失败关闭（`resolver.py:94`，被
    `test_resolver_unique_no_match_ambiguous` 钉住）。所以电机算子和
    子系统算子**不能注册进同一个 resolver**——实测会得到
    `{'ok': False, 'error': 'ambiguous', ...}`，整台设备的候选全灭。

    这里改为**按信封的 `component_hint` 分流到两条独立的 DataPlane**：每条
    各自一个 resolver、各自一个绑定，同一条链路上的网关/解析/窗口/检测一步
    不少。分流在数据面**之前**做，所以不是绕过 resolver，而是给两个不同
    部件族各配一个正确的 resolver。

    ⚠ 与生产路径的差异（必须知道）：生产里事件经 MQTT/HTTP 进入**另一个进程**
    的 `ServiceState`，候选落 DB。这里候选只在内存，进程退出即失。

    线程安全：`process_one` 从仿真线程调用，`candidates()` 从 HTTP 线程读，
    故 `_lock`。Gateway 内部还有自己的限流状态。
    """

    def __init__(self, *, device_sn: str = DEMO_DEVICE,
                 tenant_id: str = DEMO_TENANT) -> None:
        self.auth = {"tenant_id": tenant_id, "device_sn": device_sn}
        self._lock = threading.Lock()
        self.error: str | None = None
        self._planes: list[tuple[str, Any, tuple[str, ...]]] = []
        self._build_planes()
        self._candidates: list[dict[str, Any]] = []
        self._normalized = 0
        self._rejected = 0

    def _build_planes(self) -> None:
        """建两条数据面，并把各自的部件归属记下来。

        任一条建不起来都**不致命**：另一条照跑，页面上如实显示哪条缺了。
        早期版本是一条建不起来就整体不产候选，一个子系统的问题会连带
        把电机那条也打死。
        """
        from ..data_plane.p2_demo import _detector_config
        from ..data_plane.pipeline import DataPlane, build_demo_resolver
        from .emit import DEMO_COMPONENT, SUBSYSTEM_SOURCES
        from .subsystem_operator import build_subsystem_operator

        errs: list[str] = []
        for label, factory, comps in (
            ("motor", _detector_config, (DEMO_COMPONENT,)),
            ("subsystem", build_subsystem_operator, tuple(SUBSYSTEM_SOURCES)),
        ):
            try:
                self._planes.append((label, DataPlane(build_demo_resolver(factory())), comps))
            except Exception as e:          # noqa: BLE001
                errs.append(f"{label}: {type(e).__name__}: {e}")
        if errs:
            self.error = "; ".join(errs)

    def _route(self, env: dict[str, Any]):
        hint = env.get("component_hint")
        for _label, plane, comps in self._planes:
            if hint in comps:
                return plane
        return None

    def push(self, ev: SimEvent) -> None:
        if not self._planes:
            return
        env = dict(ev.env)
        plane = self._route(env)
        if plane is None:
            # 没挂算子的部件：照旧送进电机那条，让它按原有语义被解析/拒绝，
            # 而不是在这里静默吞掉——吞掉会让"为什么没有候选"变成谜。
            plane = self._planes[0][1]
        with self._lock:
            norm, err = plane.process_one(env, self.auth)
            if err is not None:
                self._rejected += 1
                return
            if norm is not None:
                self._normalized += 1
            # 每 50 个事件收一次候选：build() 是 O(组数)，
            # 每个事件都调没必要，延迟 0.5s 对演示无感。
            if self._normalized % 50 == 0:
                self._collect()

    def _collect(self) -> None:
        built: list[Any] = []
        for _label, plane, _comps in self._planes:
            built.extend(plane.builder.build())
        for c in built:
            d = c.to_dict() if hasattr(c, "to_dict") else dict(c.__dict__)
            # 去重：同一 (component, 窗口起点) 只保留最新的那条
            key = (d.get("component_ref"), d.get("window_start") or
                   d.get("occurred_at"))
            for i, old in enumerate(self._candidates):
                if (old.get("component_ref"), old.get("window_start") or
                        old.get("occurred_at")) == key:
                    self._candidates[i] = d
                    break
            else:
                self._candidates.append(d)
        self._candidates.sort(key=lambda d: str(d.get("window_start") or
                                                d.get("occurred_at") or ""),
                              reverse=True)

    def reset(self) -> None:
        """重开一局时把这局的结果清干净。

        **不清会把演示讲错**：`/api/reset` 只重建仿真，候选列表还挂着上一局
        的条目，于是"刚 reset 到 normal，页面上仍有 8 条候选"——观众会以为
        正常工况就在报故障。数据面也一并重建：窗口缓冲、去重键、限流令牌
        都按"一台设备的一次运行"来算，跨局累积会让窗口里混进上一局的点。
        """
        with self._lock:
            self._candidates.clear()
            self._normalized = 0
            self._rejected = 0
            self._planes.clear()
            self._build_planes()

    def candidates(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            self._collect()
            return self._candidates[:limit]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            planes = {}
            for label, plane, comps in self._planes:
                planes[label] = {
                    "components": list(comps),
                    "ingested": plane.stats.ingested,
                    "resolved": plane.stats.resolved,
                    "rejected": plane.stats.rejected,
                    "rejected_by_reason": dict(plane.stats.rejected_by_reason),
                }
            return {
                "available": bool(self._planes),
                "error": self.error,
                "normalized": self._normalized,
                "rejected": self._rejected,
                "planes": planes,
            }


# ---------------------------------------------------------------- 请求体
class ControlBody(BaseModel):
    speed: float | None = None
    paused: bool | None = None


class InjectBody(BaseModel):
    key: str
    severity: float | None = None
    duration_s: float | None = None
    start_s: float = 0.0


class ResetBody(BaseModel):
    posture: str | None = None
    fault: str | None = None
    severity: float | None = None
    seed: int | None = None
    fault_start_s: float | None = None
    thermal_scale: float | None = None


def _telemetry_rows(history: list[Any]) -> dict[str, Any]:
    """把最近 N 个采样的 12 关节读数转成页面能直接画的结构。

    只给**当前值 + 最近曲线**，不搬整车历史：12 关节 × 600 点 = 7200 个数字，
    每 200ms 传一次会明显吃带宽。曲线降到 120 点（12 秒 @10Hz）足够看出趋势。
    """
    if not history:
        return {"t": 0.0, "joints": [], "series": {}}
    last = history[-1]
    joints = []
    for i, name in enumerate(last.joints):
        joints.append({
            "name": name,
            "q": round(float(last.q[i]), 4),
            "dq": round(float(last.dq[i]), 4),
            "tau": round(float(last.torque[i]), 3),
            "current": round(float(last.current[i]), 2),
            "temp": round(float(last.temperature[i]), 1),
            "valid": bool(last.valid[i]),
        })
    n = 120
    tail = history[-n:]
    temp = np.nanmax(np.stack([x.temperature for x in tail]), axis=1)
    curr = np.nanmax(np.stack([x.current for x in tail]), axis=1)
    series = {
        "t": [round(float(x.t), 2) for x in tail],
        "temp": [round(float(v), 1) for v in temp],
        "current": [round(float(v), 2) for v in curr],
    }
    return {"t": round(float(last.t), 3), "joints": joints, "series": series}


def create_app(*, config: SimConfig | None = None,
               publish_hz: float = 10.0,
               mqtt_host: str | None = None, mqtt_port: int = 1883,
               mqtt_qos: int = 1,
               frame_hz: float = 5.0) -> FastAPI:
    app = FastAPI(title="Go2 仿真控制台", version="0.1.0")
    session = SimSession(config or SimConfig(posture="stand", fault="normal"),
                         publish_hz=publish_hz, frame_hz=frame_hz)
    sink = _PlaneSink()
    # MQTT 是可选的第二条出口：不配 host 就只走进程内数据面。
    # 两者**同时**投递而不是二选一——演示时页面上看候选、同时能用另一个
    # 订阅端证明数据真的上了总线。
    mqtt = MqttTelemetryPublisher(
        host=mqtt_host, port=mqtt_port, tenant_id=DEMO_TENANT,
        device_sn=DEMO_DEVICE, qos=mqtt_qos,
        enabled=bool(mqtt_host)) if mqtt_host else None

    def _deliver(ev: SimEvent) -> None:
        sink.push(ev)
        if mqtt is not None:
            mqtt.publish(ev.env)

    session._on_event = _deliver
    app.state.session = session
    app.state.sink = sink
    app.state.mqtt = mqtt

    @app.on_event("startup")
    def _startup() -> None:                 # pragma: no cover - 由 uvicorn 触发
        session.start()

    @app.on_event("shutdown")
    def _shutdown() -> None:                # pragma: no cover
        session.stop()
        if mqtt is not None:
            mqtt.close()

    # ------------------------------------------------------------ 读取
    @app.get("/api/state")
    def state() -> dict[str, Any]:
        snap = session.snapshot()
        snap["speeds"] = list(SPEEDS)
        snap["postures"] = [{"key": p.key, "label": p.label,
                             "load_note": p.load_note}
                            for p in POSTURES.values()]
        snap["data_plane"] = sink.stats()
        snap["frame"] = session.frame_state()
        snap["mqtt"] = mqtt.state() if mqtt is not None else {"enabled": False}
        snap["server_time"] = datetime.now(timezone.utc).isoformat()
        return snap

    @app.get("/api/telemetry")
    def telemetry(n: int = Query(200, ge=1, le=600)) -> dict[str, Any]:
        return _telemetry_rows(session.history(n))

    @app.get("/api/frame.jpg")
    def frame(width: int = Query(880, ge=160, le=1280),
              height: int = Query(550, ge=100, le=800)) -> Response:
        # 读缓存帧，**不在这里渲染**：MuJoCo 的 GL 上下文绑在仿真线程上，
        # 从这个（uvicorn 线程池里的）线程调 render 会直接崩进程
        # ——实测 exit 0xC0000005，日志里连请求都没记下。见 rig.render_frame。
        jpg = session.frame_jpeg(width, height)
        if not jpg:
            st = session.frame_state()
            raise HTTPException(
                503, f"画面尚未就绪（渲染 {st['hz']}Hz"
                     f"{'，错误：' + st['error'] if st['error'] else ''}）")
        return Response(jpg, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/faults")
    def faults() -> dict[str, Any]:
        return {
            "faults": [f.describe() for f in FAULT_LIBRARY],
            "postures": [{"key": p.key, "label": p.label,
                          "load_note": p.load_note}
                         for p in POSTURES.values()],
        }

    @app.get("/api/candidates")
    def candidates(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
        return {"candidates": sink.candidates(limit), "plane": sink.stats()}

    # ------------------------------------------------------------ 控制
    @app.post("/api/control")
    def control(body: ControlBody) -> dict[str, Any]:
        if body.speed is not None:
            session.set_speed(body.speed)
        if body.paused is not None:
            session.set_paused(body.paused)
        return {"ok": True, "speed": session.speed, "paused": session.paused}

    @app.post("/api/inject")
    def inject(body: InjectBody) -> dict[str, Any]:
        try:
            fault_by_key(body.key)          # 未知 key 早点报错，别静默接受
        except Exception as e:              # noqa: BLE001
            raise HTTPException(400, f"未知故障 {body.key!r}: {e}") from e
        session.inject(body.key, severity=body.severity,
                       duration_s=body.duration_s, start_s=body.start_s)
        return {"ok": True, "fault": body.key,
                "at_sim_t": round(float(session.sim.snapshot()["t"]), 3)}

    @app.post("/api/reset")
    def reset(body: ResetBody) -> dict[str, Any]:
        if body.posture is not None and body.posture not in POSTURES:
            raise HTTPException(400, f"未知姿态 {body.posture!r}")
        changes = {k: v for k, v in body.model_dump().items() if v is not None}
        session.reconfigure(**changes)
        # 候选与数据面窗口必须跟着重开，否则新一局会顶着上一局的故障列表。
        sink.reset()
        return {"ok": True, **changes}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        from .console import CONSOLE_HTML
        return CONSOLE_HTML

    return app


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    ap = argparse.ArgumentParser(description="Go2 仿真控制台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--posture", default="stand",
                    choices=sorted(POSTURES.keys()))
    ap.add_argument("--fault", default="normal")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--thermal-scale", type=float, default=1.0,
                    help="热时间加速倍率（>1 非物理，仅录像用）")
    ap.add_argument("--publish-hz", type=float, default=10.0,
                    help="投递给数据面/UI 的频率（仿真仍 100Hz）")
    ap.add_argument("--frame-hz", type=float, default=5.0,
                    help="3D 画面刷新率（0=不渲染，省 CPU）")
    ap.add_argument("--mqtt-host", default=None,
                    help="MQTT broker 地址；不给则不发 MQTT")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--mqtt-qos", type=int, default=1, choices=(0, 1))
    args = ap.parse_args(argv)

    cfg = SimConfig(posture=args.posture, fault=args.fault,
                    thermal_scale=args.thermal_scale)
    app = create_app(config=cfg, publish_hz=args.publish_hz,
                     frame_hz=args.frame_hz,
                     mqtt_host=args.mqtt_host, mqtt_port=args.mqtt_port,
                     mqtt_qos=args.mqtt_qos)
    app.state.session.set_speed(args.speed)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":                  # pragma: no cover
    raise SystemExit(main())


__all__ = ["create_app", "main"]
