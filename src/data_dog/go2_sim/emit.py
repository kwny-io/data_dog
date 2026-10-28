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
    "DEMO_COMPONENT", "VENDOR", "MODEL", "FIRMWARE", "PROTOCOL_VERSION", "EPOCH",
    "payload_for", "envelope", "envelopes", "all_envelopes", "iter_envelopes",
]
