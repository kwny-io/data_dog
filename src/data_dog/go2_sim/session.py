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

from .emit import DEMO_COMPONENT, SUBSYSTEM_SOURCES, envelope, subsystem_envelopes
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
    # 两个**不同**的丢弃来源，必须分开数。合成一个数时 1 亿的积压丢弃和
    # 3 次投递异常看不出区别，页面上也没法解释"这些帧去哪了"。
    publish_failed: int = 0   # 下游抛异常（信封没能交出去）
    sample_failed: int = 0    # 样本级出口（DDS）抛异常
    backlog_dropped: int = 0  # 追不上墙钟而主动放弃的仿真步（_MAX_STEPS_PER_TICK）
    started_at: float = 0.0
    wall_elapsed: float = 0.0
    drop_reason: str | None = None   # 首个投递失败原因（不静默）

    @property
    def dropped(self) -> int:
        return self.publish_failed + self.sample_failed + self.backlog_dropped


class SimSession:
    """一台持续运行的仿真 Go2。

    `on_event` 在**后台线程**里被调用，必须快且不能抛异常；
    抛出的异常**不拖死仿真**，但会被记下原因并计入 `stats.dropped`
    ——失败可以不致命，但不能看不见。
    """

    def __init__(self, config: SimConfig, *,
                 on_event: Callable[[SimEvent], None] | None = None,
                 on_sample: Callable[[Telemetry], None] | None = None,
                 publish_hz: float = 10.0,
                 frame_hz: float = 5.0,
                 history: int = 600) -> None:
        self.sim = Go2Sim(config)
        self._on_event = on_event
        # **每个遥测点一次**的钩子，与 `_on_event` 的"每条信封一次"分开。
        #
        # 为什么必须分开：DDS 出口发的是**一整个 `LowState_` 结构**，
        # 里面同时装着 12 个关节 + IMU + BMS + 散热。而 `_on_event` 每个
        # 遥测点要调 5 次（电机 + 4 个子系统各一条信封），挂在那里会把
        # 同一帧 LowState_ 发五遍——总线负载 ×5，而收端完全看不出异样
        # （五帧一模一样）。真机上是**一个 topic 一帧**，所以钩子按
        # "样本"计，不按"信封"计。
        self._on_sample = on_sample
        # 投递抽样：物理仍跑 100Hz，但每秒只挑 `publish_hz` 个点发出去。
        # 每个点发 **5 条信封**（1 电机 + 电池/IMU/散热/整机 4 个子系统），
        # 所以 10Hz 投递 = 50 条/s，网关默认 200/s 有余量。
        # 温度/压力曲线的信息在 10Hz 完全够，100Hz 全发纯属浪费带宽。
        #
        # **这是墙钟频率，不是仿真频率**（见 `next_pub_wall`）：不管 1x 还是
        # 15x，都是每秒 10 次。若按仿真时刻算，15x 下会变成 750 条/s 打穿限流。
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
        # reset 与 step 的互斥锁：`sim.reset()` 会换掉 MjData，而仿真线程
        # 可能正拿旧 data 步进/编码。不加锁就会把上一局的遥测投进新一局。
        self._sim_lock = threading.Lock()
        # reset 时置位，让 `_run` 把墙钟/仿真时钟基准一起归零
        self._reset_clock = threading.Event()
        # 换倍率时重锚墙钟基准（不重置仿真状态，只重置"从现在起按新倍率算"）
        self._reanchor = threading.Event()
        self._thread: threading.Thread | None = None
        self.stats = SessionStats()
        # **每个部件一条独立序列号**，不是全场共享一个计数器。
        #
        # 注意：改之前的那种共享计数器（每格 +1、5 个信封取同一个值）**今天
        # 恰好也是对的**——五个部件严格同频投递，每个部件看到的都是
        # 1,2,3,…，与拆开后的结果完全一致。这是"五个部件永远锁步"这个巧合
        # 在撑着的，不是设计给的保证。只要有一路开始不同频——只发部分子系统、
        # 某一路上降采样、`all_envelopes` 的 12 关节模式——共享计数器立刻
        # 变成跨部件的步进，`COMM_PACKET_LOSS` 就会把别人的帧读成自己的空洞。
        # 而 `WindowBuffer` 的窗口键正是 `device_sn|component`（`pipeline.py`），
        # 它本来就是按部件分窗的，序列号按部件分开才和它同构。
        # 真机上每条 topic 也各有自己的序列号，这里照实拆开。
        self._seq: dict[str, int] = {}
        self._t0 = time.monotonic()
        # 真正在推进的墙钟（暂停不算）。`actual_speed` 的分母用这个，
        # 不能用 `_t0` 起算的总墙钟——见 snapshot() 的说明。
        self._active_wall = 0.0
        # (有效墙钟, 仿真时刻) 采样点，用来算**近期**实际倍率。
        # 不能用"开天辟地以来的平均值"：从 1x 切到 15x 后，平均仍被
        # 之前的 1x 拖着，显示 4.71x 而当下真在跑 12.9x。
        self._rate_hist: deque[tuple[float, float]] = deque(maxlen=2400)
        self._rate_lock = threading.Lock()
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
        new = min(100.0, max(0.1, float(speed)))
        changed = abs(new - self.speed) > 1e-9
        self.speed = new
        if changed:
            # **必须重锚时钟**。`_run` 里算的是
            # `target = (now - t_wall0) * speed`；t_wall0 是旧倍率下的基准，
            # 直接把 speed 换成 4 倍，等于宣称"这 7 秒都该按 4 倍跑过"，
            # 立刻欠下 3×7=21 仿真秒 → 一次性丢弃 2082 个遥测步
            # （实测就是这个数）。观感上是"一按 4x 卡一下、丢一截"。
            # 置位后 `_run` 会用 `now - sim_done/speed` 重新起算，无欠账。
            self._reanchor.set()
        self._wake.set()          # 立刻按新倍率重算节奏

    def set_paused(self, paused: bool) -> None:
        self.paused = bool(paused)
        self._wake.set()

    def inject(self, key: str, **kw: Any) -> None:
        # 注入改的是故障状态表，与仿真线程的 step 互斥（见 reconfigure）。
        with self._sim_lock:
            self.sim.inject(key, **kw)

    def reconfigure(self, **changes: Any) -> None:
        """改姿态/严重度等需要重建环境的项——**会复位**，与 inject 不同。

        姿态改的是关节目标角语义，运行中切换等于换了一台机器摆的姿势，
        物理状态没有连续性可言，所以必须重建（并如实告知调用方）。

        **必须和仿真线程互斥**：`sim.reset()` 换掉内部的 MjData，
        而仿真线程可能正拿着旧的 data 在 `step()` 或编码信封。实测到的
        现象是 reset 之后页面上还会冒出上一局的候选（窗口时间是 reset
        之前的 t），因为旧 data 的遥测在 reset 之后才被投递出去。
        这里用 `_sim_lock` 把"重建"和"步进"隔开；顺带把投递基准与
        会话起点一起重置，否则 `actual_speed` 会拿重开后的 t 除以
        进程启动以来的总墙钟，显示成 0.02x（实际 4.6x）。
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
        with self._sim_lock:
            self.sim.reset(merged)
            with self._hist_lock:
                self._hist.clear()
            self._seq.clear()
            self._t0 = time.monotonic()
            self._reset_clock.set()

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

    # 一次批量步进里最多补发多少条。守住上限是为了让"卡了一下"不会
    # 紧接着变成一阵暴雨——那比掉一帧更容易打穿网关限流。
    _MAX_PUB_PER_CHUNK = 4

    def _chunk_index_at(self, slot_wall: float, sim_before: float, n: int,
                        t_wall0: float) -> int:
        """这个墙钟格子里**应有的**仿真时刻，落在这一批的第几个遥测点。

        墙钟 → 仿真时刻的换算是这个循环的核心不变量：
        `sim_t = (wall - t_wall0) * speed`。这里只是它的逆运算，
        用来把"该发的那一格"落回具体的遥测点，而不是一律取批尾——
        取批尾会让 15x 下的所有信封挤在同一小段时间里。
        """
        sim_at = (slot_wall - t_wall0) * self.speed
        # 相对这批起点偏移多少个 100Hz 遥测点
        idx = int(round((sim_at - sim_before) * 100.0))
        return max(0, min(n - 1, idx))

    def _tick_seq(self) -> dict[str, int]:
        """推进一个投递格：每个部件的序列号各自 +1，返回本格快照。

        **丢帧也要占号。** F3 丢包 / F19 坏帧的形态就是"设备发过、下游没收到"，
        编号必须照样前进——`payload_for` 返回 None 时整行不发，若此时编号也
        不涨，下游看到的序列号仍然连续，`COMM_PACKET_LOSS` 永远报不出来。
        """
        for c in (DEMO_COMPONENT, *SUBSYSTEM_SOURCES):
            self._seq[c] = self._seq.get(c, 0) + 1
        return dict(self._seq)

    def _publish(self, t_last: Telemetry, sequences: dict[str, int]) -> None:
        """一个遥测点 → 一整套信封，逐条投递给下游。异常不许静默。

        先调 `_on_sample`（**一次**，整帧入口），再走逐信封投递。
        两者**各自独立 try**：DDS 出口挂了不能连累进程内数据面和 MQTT，
        反过来也一样——演示上一路故障不该把另外两路带走。
        """
        if self._on_sample is not None:
            try:
                self._on_sample(t_last)
            except Exception as e:                # noqa: BLE001
                self.stats.sample_failed += 1
                if (self.stats.sample_failed == 1
                        or self.stats.sample_failed % 100 == 0):
                    self.stats.drop_reason = (
                        f"样本出口 {type(e).__name__}: {e} "
                        f"(共 {self.stats.sample_failed} 帧)")
        try:
            env = envelope(t_last, sequence=sequences[DEMO_COMPONENT])
            batch = [env] if env is not None else []
            # 非电机子系统（电池/IMU/散热/整机）：不接进来，检测器只有
            # 关节温度一条线可看，B2/B3 那 10 条故障永远不可能被报出——
            # 不是规则没写，是数据根本没离开仿真器。
            # 序列号按 `SUBSYSTEM_SOURCES` 的顺序逐个给，与部件一一对应。
            batch += subsystem_envelopes(
                t_last,
                components=tuple(SUBSYSTEM_SOURCES),
                sequence=[sequences[c] for c in SUBSYSTEM_SOURCES],
            )
            for one in batch:
                self._on_event(SimEvent(t=float(t_last.t), env=one))
                self.stats.published += 1
        except Exception as e:                    # noqa: BLE001
            # **不许静默**：这里曾经只有一句 `dropped += 1`，什么原因都不留。
            # 结果是页面照常跑、温度照常涨、候选一直是 0，而"多少帧在哪一步
            # 消失"完全看不见——排查时只能猜。记首个原因 + 计数。
            self.stats.publish_failed += 1
            if (self.stats.publish_failed == 1
                    or self.stats.publish_failed % 100 == 0):
                self.stats.drop_reason = (
                    f"{type(e).__name__}: {e} "
                    f"(共 {self.stats.publish_failed} 条)")

    def history(self, n: int = 200) -> list[Telemetry]:
        with self._hist_lock:
            items = list(self._hist)
        return items[-max(1, n):]

    _RATE_WINDOW_S = 5.0

    def _recent_rate(self, active_now: float) -> float | None:
        """最近 `_RATE_WINDOW_S` 有效墙钟秒内的实际倍率；样本不够返回 None。"""
        with self._rate_lock:
            hist = list(self._rate_hist)
        if len(hist) < 2:
            return None
        # 从尾部往前找第一个早于 (现在 - 窗口) 的点
        cutoff = active_now - self._RATE_WINDOW_S
        anchor = None
        for w, s in reversed(hist):
            if w <= cutoff:
                anchor = (w, s)
                break
        if anchor is None:
            anchor = hist[0]
        w0, s0 = anchor
        w1, s1 = hist[-1]
        if w1 - w0 < 1.0:            # 窗口太短，噪声大，不如不给
            return None
        return (s1 - s0) / (w1 - w0)

    def snapshot(self) -> dict[str, Any]:
        snap = self.sim.snapshot()
        wall = time.monotonic() - self._t0
        # 实际倍率 = 推进的仿真时间 ÷ **有效**墙钟。**必须如实算**：
        # 请求 15x 而测到 3.2x 的情形真实存在（见 SPEEDS 的说明），
        # 把请求值当实际值显示，就是在录像里撒谎。
        #
        # 分母**不能用 wall**：暂停期间墙钟照走、仿真停着，除以它会把
        # 倍率越拉越低。实测暂停 5 秒后页面显示 0.49x，而恢复后真实
        # 速率是 0.99x——一个"卡住了"的假象，而且越暂停越离谱。
        # 这里用后台线程累计的"真正在推进的墙钟"。
        active = self._active_wall
        # 而且要看**近期**，不是开天辟地以来的平均：从 1x 切到 15x 后，
        # 累计平均仍被之前的 1x 拖着（实测显示 4.71x，当下真在跑 12.9x）。
        # 取最近 5 个"有效墙钟秒"上的两个点算增量。
        actual = self._recent_rate(active)
        if actual is None and active > 0.5:
            # 冷启动兜底：用真实推进量（steps），不是目标时刻 t。
            actual = (self.stats.steps / 100.0) / active
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
                "publish_failed": self.stats.publish_failed,
                "sample_failed": self.stats.sample_failed,
                "backlog_dropped": self.stats.backlog_dropped,
                "drop_reason": self.stats.drop_reason,
                "wall_elapsed": round(wall, 2),
                "active_wall": round(active, 2),
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
        # 只有在**没暂停**的 tick 里才把墙钟增量记进 `_active_wall`。
        # 暂停的 0.2s 等待跳过一次记账，暂停时长就不会被算进倍率分母。
        last_tick_wall = time.monotonic()
        # 下一次投递的**墙钟**时刻（time.monotonic 绝对值）。
        #
        # 早期这里记的是仿真时刻，`next_pub += 1/publish_hz` 加的是仿真秒。
        # 那样 `publish_hz` 在变速下就变成"每仿真秒多少条"：1x 是 10Hz，
        # 15x 就是 150Hz —— 而每条遥测要发 5 个部件的信封（电机 + 电池 +
        # IMU + 散热 + 整机），15x 下就是 750 条/秒。网关令牌桶是 200/s、
        # burst 400（`gateway.py:61`），超出的部分被静默判为 rate_limited，
        # 表现成"倍率越高候选越少"，而且看不出原因。
        #
        # 投递频率本来就该跟**墙钟**走：下游是网络和消费者，它们感受的是
        # 每秒多少条，不是每仿真秒多少条。事件时间仍按仿真时钟写进信封
        # （`emit` 的 occurred_at），两件事不该混。
        #
        # 代价要讲清楚：15x 下每条信封之间跨 1.5 仿真秒，窗口里点数变少
        # （10 秒窗口只有 ~7 个点而不是 100 个）。那是数据密度本来就低，
        # 不是这里丢数据——真要 15x 下也密，得调高 publish_hz 并同步调高
        # 网关限流，那是部署决策。
        next_pub_wall = time.monotonic()

        try:
            while not self._stop.is_set():
                # 上一轮 reset 过 → 时钟基准全部归零，别把旧账背到新一局。
                if self._reset_clock.is_set():
                    self._reset_clock.clear()
                    t_wall0 = time.monotonic()
                    sim_done = 0.0
                    next_pub_wall = time.monotonic()
                    self._active_wall = 0.0
                    with self._rate_lock:
                        self._rate_hist.clear()
                    last_tick_wall = time.monotonic()
                if self.paused:
                    # 暂停期间墙钟仍在走，必须把基准一起前移，否则恢复瞬间
                    # 会认为"欠了几十秒"，一口气补跑（看起来像跳帧）。
                    # 暂停时仍按墙钟渲染：画面停在原地，但页面不会整屏黑。
                    self._render_if_due()
                    self._wake.wait(0.2)
                    self._wake.clear()
                    now = time.monotonic()
                    # 暂停的这段**不计入**有效墙钟（倍率分母），但基准要前移
                    last_tick_wall = now
                    t_wall0 = now - sim_done / max(self.speed, 1e-6)
                    # 投递基准也跟着走，否则恢复瞬间会认为"欠了一整段暂停"。
                    next_pub_wall = now + 1.0 / self.publish_hz
                    continue

                now = time.monotonic()
                self._active_wall += max(0.0, now - last_tick_wall)
                last_tick_wall = now
                # 记一个 (有效墙钟, **真实**推进的仿真时间) 点，供近期倍率用。
                #
                # **不能用 sim_done**：那是"目标时刻"，丢积压时它照样往前走
                # （`sim_done += lost/100`），用它算出来的是"以为自己跑多快"。
                # 实测 15x 下这台机器实际只跑 6.28x，而 sim_done 一路让倍率
                # 显示 14.96x——正是"把请求值当实际值"的另一种写法。
                # 真实推进量 = 实际跑过的步数，`stats.steps` 就是这个数，
                # 且不用每 tick 取一次 sim 快照。
                with self._rate_lock:
                    self._rate_hist.append(
                        (self._active_wall, self.stats.steps / 100.0))

                if self._reanchor.is_set():
                    # 换倍率：把基准挪到"已推进的仿真时间按新倍率折算"处，
                    # 这样 due 从 0 起算，不会因为倍率跳变而欠账。
                    self._reanchor.clear()
                    t_wall0 = now - sim_done / max(self.speed, 1e-6)
                    next_pub_wall = now + 1.0 / self.publish_hz

                # 本 tick 应推进到的仿真时刻
                target = (now - t_wall0) * self.speed
                due = target - sim_done
                if due > 1e-6:
                    n = int(due * 100.0)          # 100Hz 遥测点
                    if n > self._MAX_STEPS_PER_TICK:
                        # 追不上就**放弃欠账**而不是越欠越多：把基准拉回来，
                        # 让仿真时钟与墙钟重新对齐。观感是"卡了一下"，
                        # 而不是"越来越慢、永远追不上"。
                        lost = n - self._MAX_STEPS_PER_TICK
                        self.stats.backlog_dropped += lost
                        sim_done += lost / 100.0
                        n = self._MAX_STEPS_PER_TICK
                    if n > 0:
                        sim_before = sim_done
                        # 与 `reconfigure` 的 reset 互斥：否则可能拿被换掉的
                        # MjData 步进，把上一局的读数投进新一局。
                        with self._sim_lock:
                            chunk = self.sim.step(n)
                        self.stats.steps += n
                        sim_done += n / 100.0
                        with self._hist_lock:
                            self._hist.extend(chunk)
                        self._render_if_due()

                        # 投递：按**墙钟**格子从这一批遥测点里挑，见
                        # `next_pub_wall` 的说明。
                        #
                        # 一次迭代里可能跨过好几个 1/publish_hz 的格子
                        # （批量步进时一批就是 0.5 仿真秒），也可能一个都
                        # 不到。所以不能"每轮最多发一条"，要在这一批里
                        # 按格子挑点——否则实际投递频率会掉到 publish_hz
                        # 以下（实测 15x 时只剩 6 条/s 而不是 10 条/s）。
                        if chunk and self._on_event is not None:
                            now_wall = time.monotonic()
                            slots = 0
                            while (next_pub_wall <= now_wall
                                   and slots < self._MAX_PUB_PER_CHUNK):
                                slots += 1
                                # 不写成 `now + 1/hz`：那样每轮都被抬高一点点，
                                # 实际频率会漂低。按"该到点的那一格"推进。
                                slot_wall = next_pub_wall
                                next_pub_wall += 1.0 / self.publish_hz
                                idx = self._chunk_index_at(
                                    slot_wall, sim_before, n, t_wall0)
                                self._publish(chunk[idx], self._tick_seq())
                            if next_pub_wall < now_wall - 1.0:
                                # 长时间卡顿后别攒一堆欠账一次性补发
                                next_pub_wall = now_wall + 1.0 / self.publish_hz
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
