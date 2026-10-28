"""把仿真遥测转成 data_dog 的**原始日志信封**（raw-log-envelope-v1）。

为什么要有这一层
----------------
仿真器内部是 numpy 数组、部件名用 `joint_motor.front_left_hip` 这套；
而 data_dog 的入口只认一种形状：`RawLogEnvelope`（必填字段见
`data_plane/gateway.py` 的 `_REQUIRED_FIELDS`）。中间不做转换就只能绕过
网关直接写库——那样 `Gateway → Resolver → Parser → Normalizer → Detector`
整条链路都被跳过，"看 data_dog 能不能识别"这个演示就失去意义了。

关键契约（照抄网关，不自己发明）
--------------------------------
- 必填：event_id / occurred_at / received_at / sequence / vendor / model / firmware
- 身份（tenant_id / device_sn）**不写进正文**：网关一律用信道注入的身份覆盖，
  正文里写了也会被丢弃。所以本模块不产出身份字段。
- `payload_inline` 必须是 **JSON 字符串**（不是对象）：网关按文本算大小再
  `json.loads`，传 object 会被 `payload_inline_not_string` 拒掉。
- 上限 64 KiB（`_MAX_PAYLOAD_BYTES`）。这里每条载荷不到 300 字节。

载荷用 `Telemetry.component_row()`
---------------------------------
不在这里另抄一份字段名——那份映射（`_COMPONENT_TO_JOINT`）是权威定义，
两边各写一份迟早会漂。`current` 是派生量（关节扭矩 ÷ 关节侧 kt），
厂商 IDL `MotorState` 里没有这个字段，但项目既有场景也发它，故保留。
"""
from __future__ import annotations

import json
import math
import uuid
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from .rig import _JOINT_TO_COMPONENT, Telemetry

# 演示主部件：**膝盖（thigh）不是髋（hip）**。
#
# 这不是随便挑的。髋关节是腿部偏摆轴，承重和步态冲击几乎不落在它上面：
# 奔跑 + F2（散热失效）跑满 60 秒，四只 hip 最高 36.8°C，而同一时刻
# thigh 已到 129°C。检测器唯一的规则是 `temperature >= 90.0`，
# 发 hip 就等于永远不发 —— 早期把这里设成 front_left_hip，端到端跑了
# 90 秒、温度曲线漂亮地爬到 217°C，候选却始终是 0，原因就在这一行。
#
# 同一条结论在 `mujoco_sim/to_sample.py` 里已经论证过一次（站立时 calf
# 承重电流饱和、hip 偏摆不发热，只有 thigh 有真实负载且留有余量），
# 那里也是把 thigh 映射成 Agent 认的 knee 部件。两处保持一致。
DEMO_COMPONENT = "joint_motor.front_left_knee"

# 非电机子系统 → `Telemetry` 上的属性名。
#
# **为什么这些部件名不在 `topology.TOPOLOGY` 里**：那张表是**供电链**拓扑
# （battery.main → controller.main → joint_motor.*），只服务于多事件因果推理
# （`causal.py` 的"祖先"判定）。IMU / 散热 / 整机不是供电链上的节点，硬塞进去
# 会让因果推理把不相干的部件串起来。
#
# 它们不进拓扑的**代价只有一个**：Agent 的证据查询用
# `evidence.store.ALLOWED_COMPONENTS`（= TOPOLOGY 的键）做白名单，所以调查
# Agent 暂时查不到这些部件的遥测。**候选产出不受影响**——`candidate.py` 不看
# 白名单。演示要的是"检测器当场报出来"，那一条是通的。
#
# `thermal.main` 装的是 LowState 顶层的散热/通信标量（fan_frequency /
# temperature_ntc1 / bandwidth / crc_fail_rate）——IDL 里它们**不属于任何子结构**，
# 而是 LowState 自己的字段，所以单列成"散热"部件而不是塞进电池。
SUBSYSTEM_SOURCES = {
    "battery.main": "battery",
    "imu.body": "imu",
    "thermal.main": "thermal",
    "system.body": "system",
}

# 整型字段（按其厂商 IDL 类型取整，不做无意义的 float）。见下方各结构体来源。
_INT_FIELDS = frozenset({
    "soc", "status", "cycle",              # BmsState: uint8 / uint8 / uint16
    "bandwidth",                           # LowState: uint16
    "error_code", "gait_type", "sport_mode",  # SportModeState
})

VENDOR = "unitree"
MODEL = "go2"
FIRMWARE = "3.2.1"
PROTOCOL_VERSION = "motor-log-v2"

# 事件时间基准。固定值（而非 now()）是为了让同一份录像/回放可复现；
# 真正的时间轴由 `session_start + t` 给出，t 是仿真时钟。
EPOCH = datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def payload_for(t: Telemetry, component: str) -> dict[str, Any] | None:
    """一个部件的载荷。部件没有映射关节时返回 None。

    `mode` 补成 0（IDL 的 MotorState.mode，RPC/API 模式）——仿真里恒为
    RPC 模式，没有真实取值可给。

    **丢包时整行返回 None**：F3/F8/F19 把丢掉的读数写成 NaN（"没有读数"，
    不是"读数为 0"）。若照常序列化会炸在 `int(round(nan))` 上——不是报个错
    就算了，是让发布线程直接抛出、整个仿真会话中断。而丢失本身就由
    `lost` 计数器表达：跳过这一帧，下游看到的是**序列号空洞**，这正是
    真实链路丢包的样子，`COMM_PACKET_LOSS`（窗口覆盖率 <95%）要的就是它。
    补一个假温度反而会把丢包伪装成正常数据。
    """
    row = t.component_row(component)
    if row is None:
        return None
    for v in row.values():
        if not math.isfinite(v):
            return None
    out: dict[str, Any] = {"mode": 0}
    for k, v in row.items():
        if k == "temperature":
            out[k] = int(round(v))          # IDL: uint8
        elif k == "lost":
            out[k] = int(v)                 # IDL: uint8
        else:
            out[k] = round(float(v), 6)
    return out


def envelope(
    t: Telemetry,
    *,
    component: str = DEMO_COMPONENT,
    sequence: int,
    session_start: datetime = EPOCH,
    received_at: datetime | None = None,
) -> dict[str, Any] | None:
    """一条信封。`occurred_at` = 会话起点 + 仿真时钟。

    用仿真时钟而不是墙钟：变速（4x/15x）下墙钟与事件时间就脱钩了，
    下游的窗口聚合必须按事件时间算，否则 15 倍速会把 1 秒的窗塞进 15 秒的量。
    """
    payload = payload_for(t, component)
    if payload is None:
        return None
    occ = session_start + timedelta(seconds=float(t.t))
    return {
        "event_id": str(uuid.uuid4()),
        "occurred_at": _iso(occ),
        "received_at": _iso(received_at or occ),
        "sequence": int(sequence),
        "vendor": VENDOR,
        "model": MODEL,
        "firmware": FIRMWARE,
        "protocol_version": PROTOCOL_VERSION,
        "component_hint": component,
        "payload_inline": json.dumps(payload, ensure_ascii=False),
    }


def envelopes(t: Telemetry, *, sequence: int,
              components: Sequence[str] = (DEMO_COMPONENT,),
              **kw: Any) -> list[dict[str, Any]]:
    """一条遥测 → 若干条事件（每个部件一条）。

    **默认只发 `DEMO_COMPONENT`**（front_left_hip），两个硬理由：

    1. 只有它挂了算子。其余 3 个 `joint_motor.*` 部件能过网关和解析，
       但 `_detector` 为 None，`detect()` 根本不跑——发出去的每一条都是
       "被接受的死数据"，除了增加负载什么也不产生。
    2. **限流**。网关每设备默认 200 条/s（`DD_RATE_PER_SEC`），而
       4 部件 × 100Hz = 400 条/s，实测会被拒掉 94%。要发 4 个部件，
       必须同时调高 `DD_RATE_PER_SEC`——那是部署决策，不该由仿真器
       自己假定。

    需要多部件时显式传 `components=_JOINT_TO_COMPONENT.values()`，
    并确认部署已调高限流。
    """
    out = []
    for comp in components:
        env = envelope(t, component=comp, sequence=sequence, **kw)
        if env is not None:
            out.append(env)
    return out


def payload_for_subsystem(t: Telemetry, component: str) -> dict[str, Any] | None:
    """一个非电机子系统的载荷。未知部件返回 None。

    字段名**照厂商 IDL**，不改名、不合并：

    - 电池 `battery.main` → `BmsState` 的 `soc / status / cycle / bq_ntc /
      mcu_ntc / cell_vol`，外加 `LowState` 顶层的 `power_v / power_a`
      （母线电压 / 母线电流）。
      **总线电流用 `power_a`，不用 `current`**——`BmsState.current` 和项目
      载荷里表示"每关节电机电流"的 `current` 同名不同义。实测：若把总线和
      电机两个 `current` 声明成同一个 source，Parser 会把 95A 的电机电流
      原样写进电池判据（`chain.py:36` 按 `source` 取值，name 不同不影响）。
      这里用厂商自己的名字 `power_a` 就**结构上不可能撞**，比自造一个
      `bus_current` 更贴 IDL。
    - IMU `imu.body` → `IMUState` 里**已经是标量**的那些：`roll/pitch/yaw`
      （由 `rpy` 数组展平）、`gyro_x/y/z`、`accel_x/y/z`（由 `gyroscope` /
      `accelerometer` 展平）、`accel_norm`、`imu_temp`。
    - 散热 `thermal.main` → `LowState` 顶层 `fan_frequency / temperature_ntc1 /
      bandwidth / crc_fail_rate`。
    - 整机 `system.body` → `SportModeState` 的步态与姿态标量。

    **数组字段（`cell_vol` / `foot_force` / `foot_speed_body`）不进载荷**：
    `Parser._coerce` 不展平数组、`WindowBuffer` 也只收标量（`chain.py:142`）。
    因此 `subsystems.py` 已把它们各自展开成 `cell_v_max/min/spread` 与
    `foot_force_*` / `foot_speed_*` —— 展平发生在**仿真侧**，不是解析侧。

    **已知的两处类型不符（模块级明说，不在载荷里偷偷改）**：
    `temperature_ntc1` 与 `imu_temp` 在 IDL 里是 `uint8`（0–255，无负数），
    而仿真器把 25.5°C 这类带小数的值发出来。载荷按实际物理量发 float，
    取整只在**声明字段类型时**做取舍；演示里读的是"散热口温度"这个物理量。
    """
    attr = SUBSYSTEM_SOURCES.get(component)
    if attr is None:
        return None
    state = getattr(t, attr, None)
    if state is None or not hasattr(state, "as_metrics"):
        return None
    metrics = state.as_metrics()

    # 电池：总线电压/电流改回 LowState 的厂商名，并去掉会引起同名歧义的 `current`。
    if attr == "battery":
        metrics.pop("current", None)      # 与关节电流同名，见上
    # 整机：`pos_*` / `vel_*` / `speed` 属于导航，对故障判定无用；
    # 留在载荷里只会让每条信封更大，判定上一点用没有。
    if attr == "system":
        for k in ("pos_x", "pos_y", "vel_x", "vel_y", "vel_z", "speed"):
            metrics.pop(k, None)

    out: dict[str, Any] = {}
    for k, v in metrics.items():
        f = float(v)
        if not math.isfinite(f):
            return None                   # 任何一项非有限 → 整行不发（同电机侧）
        out[k] = int(round(f)) if k in _INT_FIELDS else round(f, 6)
    return out


def subsystem_envelopes(
    t: Telemetry,
    *,
    sequence: int,
    components: Sequence[str] = tuple(SUBSYSTEM_SOURCES),
    session_start: datetime = EPOCH,
    received_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """一次遥测 → 若干条非电机子系统的信封。

    与 `envelopes()` 分开而不是揉进去：电机部件的载荷来自
    `Telemetry.component_row()`（逐关节、带 `lost` 语义），子系统载荷来自
    各 `State.as_metrics()`（整机一份）。两种取数方式不同，硬塞进一个函数
    会让"这一条到底是哪个关节"变得含糊。

    默认发全部四个子系统 = 每个投递点 4 条。**这是限流账**：10Hz 投递
    × 4 = 40 条/s，网关默认 200/s，余量够；若把 `publish_hz` 提到 50Hz，
    4 × 50 = 200/s 正好顶满，得同时调 `DD_RATE_PER_SEC`。
    """
    occ = session_start + timedelta(seconds=float(t.t))
    got = _iso(occ)
    out: list[dict[str, Any]] = []
    for comp in components:
        payload = payload_for_subsystem(t, comp)
        if payload is None:
            continue
        out.append({
            "event_id": str(uuid.uuid4()),
            "occurred_at": got,
            "received_at": _iso(received_at or occ),
            "sequence": int(sequence),
            "vendor": VENDOR,
            "model": MODEL,
            "firmware": FIRMWARE,
            "protocol_version": PROTOCOL_VERSION,
            "component_hint": comp,
            "payload_inline": json.dumps(payload, ensure_ascii=False),
        })
    return out


def all_envelopes(t: Telemetry, *, sequence: int, **kw: Any
                  ) -> list[dict[str, Any]]:
    """四个已映射部件全发。**需部署把 DD_RATE_PER_SEC 调到 ≥500**，
    否则会被限流拒掉大部分（见 `envelopes` 的说明）。"""
    return envelopes(t, sequence=sequence,
                     components=list(_JOINT_TO_COMPONENT.values()), **kw)


def iter_envelopes(chunks: Iterator[list[Telemetry]], *,
                   start_sequence: int = 1, **kw: Any
                   ) -> Iterator[dict[str, Any]]:
    """把 `sim.step(n)` 的输出流压成事件流。"""
    seq = start_sequence
    for chunk in chunks:
        for t in chunk:
            yield from envelopes(t, sequence=seq, **kw)
            seq += 1


__all__ = [
    "DEMO_COMPONENT", "SUBSYSTEM_SOURCES", "VENDOR", "MODEL", "FIRMWARE",
    "PROTOCOL_VERSION", "EPOCH",
    "payload_for", "envelope", "envelopes", "all_envelopes", "iter_envelopes",
    "payload_for_subsystem", "subsystem_envelopes",
]
