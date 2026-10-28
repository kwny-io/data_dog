"""仿真会话：把 `Go2Sim` 包成一个**按墙钟持续推进**的后台对象。

为什么需要会话层
----------------
`Go2Sim.step(n)` 是"算 n 步"，不知道真实时间。演示要的是"打开页面狗就在动"，
于是需要有人按真实节奏去调它——这就是本层。它同时负责：

- **变速**：1x / 4x / 15x。倍率只改"每秒推进多少仿真秒"，不改物理。
- **实时投递**：按事件时间把信封推给下游（HTTP 聚合视图 / MQTT），
  而不是攒够一批再吐。
- **运行中注入**：换故障不重启（走 `Go2Sim.inject`），温度/位姿连续。

线程模型
--------
一个后台线程，靠 `Event` 控制休眠——**不用 sleep(1/speed)**：
倍率变化、暂停要能立刻生效，固定 sleep 会让变倍率等一整个周期。
`_wake` 事件被 set 时线程立刻重新算节奏。

仿真步与遥测的关系
------------------
`TELEMETRY_HZ=100`，物理步长 2ms，所以 1 个遥测点 = 5 个物理步（见 rig.py）。
本层只按遥测点推进：`step(1)` 一次。

诚实边界：**1x 是全速物理的 1/1**，即 1 秒墙钟推进 1 秒仿真。实测引擎能跑到
25x，所以 15x 倍率有充足余量；但 15x 下每个遥测点之间的墙钟间隔只有
0.67ms，HTTP/ MQTT 的投递开销可能撑不住——所以投递做了抽样（见 `publish_hz`）。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .emit import envelope
from .rig import Go2Sim, SimConfig, Telemetry

# 可选倍率。上限 15x 的依据是实测**空闲机器**上的吞吐：
#   站立 23.5x（2353 步/秒）、奔跑 16x（1600 步/秒）。
# 留 40% 余量给渲染与序列化，故取 15x。
#
# ⚠ 这不是"15x 一定能达到"。实测过两个会把它拉垮的因素，都记在这里：
#   1. **CPU 竞争**：同机再跑一个仿真实例（比如批量生成数据集）时，
#      奔跑姿态会从 1600 掉到 317 步/秒（3.2x）——倍率直接撞天花板。
#   2. **姿态**：奔跑的接触求解比站立贵约 1.5 倍（步态每步都在换腿着地）。
# 所以 UI 上显示的"实际倍率"取 `sim.snapshot()["t"] / wall_elapsed`，
# 不拿请求倍率冒充——达不到就如实显示 3.2x。
SPEEDS = (1.0, 4.0, 15.0)


@dataclass
class SimEvent:
    """一条已编码好的信封 + 它的仿真时刻（UI 与 MQTT 共用）。"""

    t: float
    env: dict[str, Any]


@dataclass
class SessionStats:
    steps: int = 0
    published: int = 0
    dropped: int = 0          # 投递失败（下游抛异常 / 跟不上）
    started_at: float = 0.0
    wall_elapsed: float = 0.0
    drop_reason: str | None = None   # 首个投递失败原因（不静默）


class SimSession:
    """一台持续运行的仿真 Go2。

    `on_event` 在**后台线程**里被调用，必须快且不能抛异常；
    抛出的异常**不拖死仿真**，但会被记下原因并计入 `stats.dropped`
    ——失败可以不致命，但不能看不见。
    """

    def __init__(self, config: SimConfig, *,
                 on_event: Callable[[SimEvent], None] | None = None,
                 publish_hz: float = 10.0,
                 frame_hz: float = 5.0,
                 history: int = 600) -> None:
        self.sim = Go2Sim(config)
        self._on_event = on_event
        # 投递抽样：100Hz 全发会让 4 个部件 × 100 = 400 msg/s，
        # 既撞网关限流又没必要（温度曲线的信息在 10Hz 完全够）。
        # 仿真本身仍是 100Hz——抽样只影响"发出去多少"，不影响物理。
        self.publish_hz = max(0.5, float(publish_hz))
        self._pub_every = max(1, int(round(100.0 / self.publish_hz)))
        self.speed = 1.0
        self.paused = False
        # 帧缓存：**渲染只能在仿真线程做**（MuJoCo 的 GL 上下文绑线程，
        # 从 uvicorn 请求线程调会访问冲突崩进程，实测 exit 0xC0000005）。
        # 于是仿真线程按 frame_hz 渲一帧存这儿，HTTP 只读字节。
        self.frame_hz = max(0.0, float(frame_hz))
        self._frame = b""
        self._frame_t = 0.0
        self._frame_lock = threading.Lock()
        self._last_render = 0.0
        # 单次渲染的尺寸。页面只有一块画面，尺寸变化意味着换设备/改窗口，
        # 演示里不会频繁发生，所以按需更新而不做多档缓存。
        self._frame_size = (880, 550)
        self._frame_error: str | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.stats = SessionStats()
        self._seq = 0
        self._t0 = time.monotonic()
        # 环形缓冲：UI 拉取最近 N 个点的遥测（不需要自己存历史）
        self._hist: deque[Telemetry] = deque(maxlen=history)
        self._hist_lock = threading.Lock()

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.stats.started_at = time.monotonic()
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="go2sim",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """停线程并释放渲染器。

        `sim.close()` 不能在这里直接调：它要销毁仿真线程建的 GL 上下文，
        跨线程做会段错误。仿真线程在 `_run` 的 finally 里自己收尾。
        这里只兜底——线程如果压根没起来（没建过 GL），才由本线程关。
        """
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            alive = self._thread.is_alive()
            self._thread = None
            if alive:
                return          # 没停干净就别碰 GL，留给进程退出
        self.sim.close()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---------------------------------------------------------------- 控制
    def set_speed(self, speed: float) -> None:
        self.speed = min(100.0, max(0.1, float(speed)))
        self._wake.set()          # 立刻按新倍率重算节奏

    def set_paused(self, paused: bool) -> None:
        self.paused = bool(paused)
        self._wake.set()

    def inject(self, key: str, **kw: Any) -> None:
        self.sim.inject(key, **kw)

    def reconfigure(self, **changes: Any) -> None:
        """改姿态/严重度等需要重建环境的项——**会复位**，与 inject 不同。

        姿态改的是关节目标角语义，运行中切换等于换了一台机器摆的姿势，
        物理状态没有连续性可言，所以必须重建（并如实告知调用方）。
        """
        cfg = self.sim.config
        merged = SimConfig(
            posture=changes.get("posture", cfg.posture),
            fault=changes.get("fault", cfg.fault),
            severity=changes.get("severity", cfg.severity),
            seed=changes.get("seed", cfg.seed),
            fault_start_s=changes.get("fault_start_s", cfg.fault_start_s),
            fault_duration_s=changes.get("fault_duration_s", cfg.fault_duration_s),
            thermal_scale=changes.get("thermal_scale", cfg.thermal_scale),
        )
        self.sim.reset(merged)
        with self._hist_lock:
            self._hist.clear()
        self._seq = 0

    # ---------------------------------------------------------------- 读取
    # ---------------------------------------------------------------- 画面
    def set_frame_size(self, width: int, height: int) -> None:
        """投票请求的尺寸；实际由仿真线程在下一帧采用（渲染不能跨线程）。"""
        w = max(64, min(int(width), Go2Sim.OFFSCREEN_W))
        h = max(48, min(int(height), Go2Sim.OFFSCREEN_H))
        self._frame_size = (w, h)

    def frame_jpeg(self, width: int | None = None,
                   height: int | None = None) -> bytes:
        """取最近一帧 JPEG。**不触发渲染**——渲染在仿真线程里跑。

        返回空字节表示还没有帧（刚启动，或渲染在环境里根本不可用），
        调用方应据此显示占位而不是当作错误。

        请求尺寸与缓存帧不同就在**本线程**用 PIL 缩放：纯像素操作，
        不碰 GL，跨线程安全。同时把尺寸记为下次渲染的目标，几帧之后
        缓存帧本身就是这个尺寸，缩放自然退化。
        """
        with self._frame_lock:
            data = self._frame
            cached = tuple(self._frame_size)
        if width is None and height is None:
            return data
        w = width if width is not None else cached[0]
        h = height if height is not None else cached[1]
        # 先比后改：set_frame_size 会把 _frame_size 更新成请求值，
        # 先改再比就永远相等，缩放分支一次都进不去。
        if not data or (w, h) == cached:
            self.set_frame_size(w, h)
            return data
        self.set_frame_size(w, h)
        import io

        from PIL import Image
        try:
            out = Image.open(io.BytesIO(data))
            # 等比铺满再居中裁切，避免把狗拉变形
            scale = max(w / out.width, h / out.height)
            mid = out.resize((max(1, round(out.width * scale)),
                              max(1, round(out.height * scale))), Image.LANCZOS)
            left = (mid.width - w) // 2
            top = (mid.height - h) // 2
            buf = io.BytesIO()
            mid.crop((left, top, left + w, top + h)).save(buf, "JPEG", quality=82)
            return buf.getvalue()
        except Exception:                   # noqa: BLE001
            return data                     # 缩放失败宁可给原帧，不报错

    def frame_state(self) -> dict[str, Any]:
        with self._frame_lock:
            return {"t": round(self._frame_t, 3), "bytes": len(self._frame),
                    "size": list(self._frame_size), "error": self._frame_error,
                    "hz": self.frame_hz}

    def _render_if_due(self) -> None:
        """仿真线程内调用。渲染慢就降频，绝不阻塞物理推进。

        按**墙钟**节流而不是仿真时刻：15x 下仿真时刻跑得飞快，
        按仿真时刻会变成 75 帧/秒，白白吃掉一半吞吐（渲染约 20ms/帧）。
        """
        if self.frame_hz <= 0:
            return
        now = time.monotonic()
        if now < self._last_render + 1.0 / self.frame_hz:
            return
        self._last_render = now
        w, h = self._frame_size
        t = self.sim.snapshot()["t"]
        try:
            jpg = self.sim.render_jpeg(width=w, height=h)
        except Exception as e:              # noqa: BLE001
            # 无 GL 的机器上不该让整个演示挂掉：记一次原因，之后不再重试。
            with self._frame_lock:
                self._frame_error = f"{type(e).__name__}: {e}"
            self.frame_hz = 0.0
            return
        with self._frame_lock:
            self._frame = jpg
            self._frame_t = float(t)
            self._frame_error = None

    def history(self, n: int = 200) -> list[Telemetry]:
        with self._hist_lock:
            items = list(self._hist)
        return items[-max(1, n):]

    def snapshot(self) -> dict[str, Any]:
        snap = self.sim.snapshot()
        wall = time.monotonic() - self._t0
        # 实际倍率 = 推进的仿真时间 ÷ 墙钟。**必须如实算**：
        # 请求 15x 而测到 3.2x 的情形真实存在（见 SPEEDS 的说明），
        # 把请求值当实际值显示，就是在录像里撒谎。
        actual = (float(snap["t"]) / wall) if wall > 0.5 else None
        snap.update({
            "speed": self.speed,
            "actual_speed": round(actual, 2) if actual is not None else None,
            "paused": self.paused,
            "running": self.running,
            "publish_hz": self.publish_hz,
            "stats": {
                "steps": self.stats.steps,
                "published": self.stats.published,
                "dropped": self.stats.dropped,
                "drop_reason": self.stats.drop_reason,
                "wall_elapsed": round(wall, 2),
            },
        })
        return snap

    # ---------------------------------------------------------------- 循环
    # 每个调度 tick 的墙钟长度。**不能按"一个遥测点"去等**：
    # 15x 时一个点只占 0.67ms，而 Windows 定时器最小粒度是 ~1ms（常见 15.6ms），
    # `Event.wait(0.00067)` 实测被抬到毫秒级，于是无论请求多少倍率都撞在
    # ~3x 的天花板上（实测：4x→2.96x，15x→3.07x）。
    # 改成"每 5ms 醒来一次，按墙钟算欠了多少仿真步，一次批量补齐"：
    # 等待粒度是可实现的 5ms，批量步进则是纯计算。
    _TICK_S = 0.005
    # 一次 tick 最多补多少遥测步（=0.5 仿真秒）。防止长时间卡顿后
    # 一次性跑几万步把 UI 冻住；超出部分丢弃并记 dropped。
    _MAX_STEPS_PER_TICK = 50

    def _run(self) -> None:
        t_wall0 = time.monotonic()
        sim_done = 0.0          # 已推进的仿真时间（秒）
        next_pub = 0.0          # 下一次投递的仿真时刻

        try:
            while not self._stop.is_set():
                if self.paused:
                    # 暂停期间墙钟仍在走，必须把基准一起前移，否则恢复瞬间
                    # 会认为"欠了几十秒"，一口气补跑（看起来像跳帧）。
                    # 暂停时仍按墙钟渲染：画面停在原地，但页面不会整屏黑。
                    self._render_if_due()
                    self._wake.wait(0.2)
                    self._wake.clear()
                    t_wall0 = time.monotonic() - sim_done / max(self.speed, 1e-6)
                    continue

                # 本 tick 应推进到的仿真时刻
                target = (time.monotonic() - t_wall0) * self.speed
                due = target - sim_done
                if due > 1e-6:
                    n = int(due * 100.0)          # 100Hz 遥测点
                    if n > self._MAX_STEPS_PER_TICK:
                        # 追不上就**放弃欠账**而不是越欠越多：把基准拉回来，
                        # 让仿真时钟与墙钟重新对齐。观感是"卡了一下"，
                        # 而不是"越来越慢、永远追不上"。
                        lost = n - self._MAX_STEPS_PER_TICK
                        self.stats.dropped += lost
                        sim_done += lost / 100.0
                        n = self._MAX_STEPS_PER_TICK
                    if n > 0:
                        chunk = self.sim.step(n)
                        self.stats.steps += n
                        sim_done += n / 100.0
                        with self._hist_lock:
                            self._hist.extend(chunk)
                        self._render_if_due()

                        # 投递：按**仿真时刻**节流，不按步数——
                        # 步数节流在变速时会跟着变频率（15x 就成了 150Hz）。
                        if chunk and self._on_event is not None:
                            t_last = chunk[-1]
                            if float(t_last.t) >= next_pub:
                                next_pub = float(t_last.t) + 1.0 / self.publish_hz
                                self._seq += 1
                                try:
                                    env = envelope(t_last, sequence=self._seq)
                                    if env is not None:
                                        self._on_event(SimEvent(t=float(t_last.t),
                                                                env=env))
                                        self.stats.published += 1
                                except Exception as e:    # noqa: BLE001
                                    # **不许静默**：这里曾经只有一句
                                    # `dropped += 1`，什么原因都不留。结果是
                                    # 页面照常跑、温度照常涨、候选一直是 0，
                                    # 而"多少帧在哪一步消失"完全看不见——
                                    # 排查时只能猜。记首个原因 + 计数。
                                    self.stats.dropped += 1
                                    if self.stats.dropped == 1 or \
                                            self.stats.dropped % 100 == 0:
                                        self.stats.drop_reason = (
                                            f"{type(e).__name__}: {e}"
                                            f" (共 {self.stats.dropped} 帧)")
                elif due < -0.5:
                    # 倍率被调低后 target 会落后于已推进量，重设基准
                    t_wall0 = time.monotonic() - sim_done / max(self.speed, 1e-6)

                # 固定粒度等待；变速/暂停会 set _wake 提前打断
                self._wake.wait(self._TICK_S)
                self._wake.clear()
        finally:
            # **必须在本线程释放渲染器**：它持有本线程建的 GL 上下文，
            # 交给别的线程析构会段错误（实测 exit 139）。
            self.sim.close()


__all__ = ["SimSession", "SimEvent", "SessionStats", "SPEEDS"]
