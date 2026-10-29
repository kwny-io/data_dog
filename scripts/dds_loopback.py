"""DDS 环回自检：把仿真数据组装成 `LowState_`，发出去再收回来逐字段核对。

为什么需要这个脚本
------------------
`tests/test_dds_bridge.py` 只验**纯映射**（槽位顺序、槽数、量纲），
它刻意不 import cyclonedds，所以 CI 不装 DDS 也能跑。
但"纯映射对"不等于"能真的发出去"——序列化、定长数组布局、DDS 类型注册，
这些只有**真的走一遍 DDS** 才暴露。

本脚本补的正是这一段：自建 publisher + subscriber 走同一通道，
把收回来的帧与仿真源值逐字段比对。

**这是协议层唯一能在没有真机时做的验证。** 它证明"桥接代码正确"，
不证明"真机数据能对上"——后者必须要真实帧。

依赖
----
    pip install 'data-dog[dds]'
    pip install unitree_sdk2py       # 或 git clone 后 pip install --no-deps .

注意 `unitree_sdk2py` 的 `setup.py` 钉死 `cyclonedds==0.10.2`，该版本在
Windows 上没有预编译 wheel，`pip install unitree_sdk2py` 会因编译失败而中止。
正确做法：先装 `cyclonedds` 的预编译 wheel（新版本即可），再
`pip install --no-deps unitree_sdk2py`。详见 `vendor_docs/unitree/SOURCES.md`。

用法
----
    python scripts/dds_loopback.py                  # 站立姿态，正常工况
    python scripts/dds_loopback.py --fault F2       # 指定故障
    python scripts/dds_loopback.py --steps 200      # 多跑几步再取帧
    python scripts/dds_loopback.py --domain 0       # DDS 域（默认 0）

退出码：0 = 全部字段对齐；1 = 有字段不一致或没收到帧。
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Sequence as _SEQ
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_dog.go2_sim.dds_bridge import (  # noqa: E402
    _SLOT_OF_JOINT,
    DdsTelemetryPublisher,
    build_low_state_kwargs,
)
from data_dog.go2_sim.rig import Go2Sim, SimConfig  # noqa: E402

TOPIC = "rt/lowstate"


def _die(msg: str) -> int:
    print(f"[FAIL] {msg}")
    return 1


def _to_plain(v):
    """把 DDS 消息/定长数组摊成纯 Python 结构，才能与 kwargs 逐字段比。

    为什么需要它：`LowState_` 里嵌着 `MotorState_`/`IMUState_`/`BmsState_`
    三个子结构，另有 4 个定长数组。只按名字抽查几项（`q`、`soc`、`power_v`）
    看着像"验过了"，实际有十几个字段从没比过——写错的 `wireless_remote`
    或 `foot_force` 一样能蒙混过关。摊平之后是**全字段**比对。

    实测发现（2026-10-30）：**`uint8` 定长数组过一趟 DDS 回来会变成 `bytes`**，
    不是 list。`head` / `sn` 之外的 `bq_ntc`、`mcu_ntc`、`wireless_remote`
    都是这样。这是 CycloneDDS 的序列化表示，不是错误——所以这里统一摊回
    list of int 再比，否则"类型不同"会把真实差异淹没。
    """
    if hasattr(v, "__dataclass_fields__"):
        return {k: _to_plain(getattr(v, k)) for k in v.__dataclass_fields__}
    if isinstance(v, (bytes, bytearray)):
        return list(v)
    if isinstance(v, _SEQ) and not isinstance(v, str):
        return [_to_plain(x) for x in v]
    return v


def _diff(path: str, want, got, out: list[str], *, tol: float = 1e-5) -> None:
    """递归比对"发出去的 kwargs"与"收回来的帧"。差异写进 out。"""
    if isinstance(want, dict):
        if not isinstance(got, dict):
            out.append(f"{path}: 期望 dict，收到 {type(got).__name__}")
            return
        if set(want) != set(got):
            out.append(f"{path}: 字段集不一致 缺={sorted(set(want) - set(got))} "
                       f"多={sorted(set(got) - set(want))}")
        for k in sorted(set(want) & set(got)):
            _diff(f"{path}.{k}", want[k], got[k], out, tol=tol)
        return
    if isinstance(want, list):
        if not isinstance(got, list):
            out.append(f"{path}: 期望数组，收到 {type(got).__name__}")
            return
        if len(want) != len(got):
            # 定长数组长度变了就是序列化布局变了，不是"少几个字段"那么轻
            out.append(f"{path}: 长度不一致 发 {len(want)} 收 {len(got)}")
            return
        for i, (a, b) in enumerate(zip(want, got, strict=True)):
            _diff(f"{path}[{i}]", a, b, out, tol=tol)
        return
    if isinstance(want, float) or isinstance(got, float):
        try:
            # float64 → float32 有精度损失，故用相对+绝对容差（纯绝对容差
            # 在 `tau_est` 这类大数值上会假红）。int 字段不会走到这一支。
            if not math.isclose(float(want), float(got),
                                rel_tol=tol, abs_tol=tol):
                out.append(f"{path}: 发 {want!r} 收 {got!r}")
        except (TypeError, ValueError):
            out.append(f"{path}: 类型不一致 发 {want!r} 收 {got!r}")
        return
    if want != got:
        out.append(f"{path}: 发 {want!r} 收 {got!r}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DDS 环回自检")
    ap.add_argument("--posture", default="stand")
    ap.add_argument("--fault", default="normal")
    ap.add_argument("--steps", type=int, default=50,
                    help="先跑多少遥测步再取最后一帧")
    ap.add_argument("--domain", type=int, default=0)
    ap.add_argument("--interface", default=None,
                    help="网卡名（多网卡机器指定，避免走错）")
    ap.add_argument("--timeout", type=float, default=3.0,
                    help="等一帧的最长秒数")
    args = ap.parse_args(argv)

    try:
        from unitree_sdk2py.core.channel import (
            ChannelFactoryInitialize,
            ChannelSubscriber,
        )
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_
    except ModuleNotFoundError as e:
        return _die(
            f"缺少 {e.name}。装法：pip install 'data-dog[dds]' 后\n"
            f"       pip install --no-deps unitree_sdk2py\n"
            f"       （别直接 pip install unitree_sdk2py，它钉的 cyclonedds==0.10.2\n"
            f"        在 Windows 上编译不过，见 vendor_docs/unitree/SOURCES.md）")

    # ---- 1) 跑真仿真，取最后一帧 -------------------------------------
    sim_cfg = SimConfig(posture=args.posture, fault=args.fault)
    sim = Go2Sim(sim_cfg)
    tels = sim.step(max(1, args.steps))
    if not tels:
        return _die("仿真没有产出任何遥测")
    tel = tels[-1]
    print(f"[1/4] 仿真就绪：t={tel.t:.3f}s  posture={args.posture}  fault={args.fault}")

    # ---- 2) 订阅端先就位 ---------------------------------------------
    # **必须先 ChannelFactoryInitialize**：订阅者构造时就要拿进程级
    # participant，没初始化就是 None，报 `'NoneType' has no attribute '_ref'`。
    try:
        ChannelFactoryInitialize(args.domain, args.interface)
    except Exception as e:                  # noqa: BLE001
        return _die(f"ChannelFactoryInitialize 失败：{type(e).__name__}: {e}")

    got: list = []
    sub = ChannelSubscriber(TOPIC, LowState_)
    sub.Init(lambda m: got.append(m), 10)
    time.sleep(0.3)                          # 等发现（discovery）完成
    print(f"[2/4] 已订阅 {TOPIC}（domain={args.domain}）")

    # ---- 3) 桥接发布 --------------------------------------------------
    pub = DdsTelemetryPublisher(topic=TOPIC, domain=args.domain,
                                interface=args.interface)
    st = pub.state()
    if not st["connected"]:
        return _die(f"DDS 出口未就绪：{st['last_error']}")
    if not pub.publish_telemetry(tel, tick=int(tel.t * 100)):
        return _die(f"发布失败：{pub.state()['last_error']}")
    print("[3/4] 已发布一帧 LowState_")

    deadline = time.time() + args.timeout
    while not got and time.time() < deadline:
        time.sleep(0.05)
    if not got:
        pub.close()
        return _die(f"{args.timeout}s 内未收到帧（通道对不上或域不同）")

    m = got[0]
    pub.close()
    print(f"[4/4] 收到帧，tick={m.tick}，开始逐字段核对\n")

    # ---- 4) 全字段比对 -------------------------------------------------
    # 拿**发出去的那份 kwargs**（同一份数据、同一函数）当基准，与收回来的
    # 帧逐字段递归比。刻意不手写"抽查几项"——那种写法漏掉的字段永远不会红，
    # 而写错的 `wireless_remote` / `foot_force` / `adc_reel` 照样能蒙混过去。
    sent_kwargs = build_low_state_kwargs(tel)
    assert sent_kwargs is not None, "仿真给了遥测，映射不该返回 None"
    sent_kwargs["tick"] = int(tel.t * 100)

    failures: list[str] = []
    _diff("LowState_", sent_kwargs, _to_plain(m), failures)

    # 上面那条是"桥接自洽"（发的 == 收的），它**抓不到"桥接从头就填错"**——
    # 映射把 FL 填进 slot 0、两边一样错，diff 照样全绿。所以另拿原始 Telemetry
    # 直接比，才能证明槽位顺序与单位换算真的对。
    idx = {j: i for i, j in enumerate(tel.joints)}
    print("  12 关节逐槽（真机槽位顺序，直接与仿真源值比）")
    for joint, slot in sorted(_SLOT_OF_JOINT.items(), key=lambda kv: kv[1]):
        i = idx[joint]
        for field, src in (("q", tel.q[i]), ("dq", tel.dq[i]),
                           ("tau_est", tel.torque[i])):
            s = float(src)
            r = float(getattr(m.motor_state[slot], field))
            if abs(s - r) > 1e-4:
                failures.append(
                    f"slot {slot} {joint}.{field}: 源 {s:.6f} 收 {r:.6f}")
        print(f"    slot {slot:2d} {joint:9s} q={m.motor_state[slot].q:+.5f} "
              f"temp={m.motor_state[slot].temperature}")

    print("\n  未使用的 8 个电机槽（12–19）应为全零")
    zero_ok = True
    for i in range(12, 20):
        if m.motor_state[i].q != 0.0 or m.motor_state[i].temperature != 0:
            zero_ok = False
            failures.append(f"零槽 {i} 非零: q={m.motor_state[i].q}")
    print(f"    校验完成：{'全部为 0' if zero_ok else '有非零'}")

    if tel.battery is not None:
        cells = list(m.bms_state.cell_vol)
        if cells[6:] != [0] * 9:
            failures.append("cell_vol 的 9 个空槽不是 0（会被均衡规则误读）")
        print(f"  BMS: soc={m.bms_state.soc} power_v={m.power_v:.3f} "
              f"cell_vol[:6]={cells[:6]} 空槽={cells[6:]}")

    print(f"\n  crc={m.crc}（置零而非编造：没有真机可对账，见模块 docstring）")

    # 覆盖度自证：真的比了 22 个顶层字段，不是一个都没比
    top = list(sent_kwargs)
    print(f"  已逐字段比对 {len(top)} 个顶层字段 / "
          f"{len(m.__dataclass_fields__)} 个 IDL 字段：{', '.join(top)}")

    if failures:
        print(f"\n[FAIL] {len(failures)} 处不一致：")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\n[OK] 全部字段对齐（22 个顶层字段逐字段递归往返）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
