"""在线链故障覆盖实测：把每类故障喂进**真正在线**的数据面，看命中哪条规则。

跑法：
    python scripts/online_fault_coverage.py
    python scripts/online_fault_coverage.py --json

为什么不能用离线 Fixture 代替
------------------------------
Fixture 回放只证明"规则表达式在给定窗口上能算出正确布尔值"。它绕过了网关、兼容绑定、
Parser、窗口缓冲、基线与候选合并——而窗口/基线类判据的失效**恰恰发生在那一层**
（此前 DSL 规则压根没被搬进在线 Detector：离线全绿、线上全哑）。所以本脚本走
`DataPlane.process_one`，与生产同一条链；只是把 Detector.detect 的**返回值**录一份，
不改任何判定逻辑。

口径
----
- 数据来自项目自己的物理模型 `synth.generator`（一阶热模型 + 故障注入），
  不是手编数字；10Hz，故障窗 [40%, 90%)。
- 冷启动：先喂若干正常样本让基线成形（Enricher 需 5 条才开放基线）。
- 判定看**命中哪条规则**，不只看"有没有报警"——否则一条永远为真的假规则也能刷满分。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_dog.core.console import ensure_utf8_console  # noqa: E402
from data_dog.data_plane.p2_demo import _detector_config  # noqa: E402
from data_dog.data_plane.pipeline import DataPlane, build_demo_resolver  # noqa: E402
from data_dog.state.memory import InMemoryStateStore  # noqa: E402
from data_dog.synth.generator import FAULT_DEFS, SynthGenConfig, generate_sample  # noqa: E402

DT = 0.1                 # 10Hz —— 与现场日志同量级，也让 window.span() 有真实秒数

# ---- 量纲适配：synth 的电流用电机轴 kt，检测规则用关节侧 kt ----
# 见 `_emit` 的说明。两个常数各自的出处：
#   KT_SYNTH = synth/generator.DEFAULT_PARAMS["kt"]（=0.25，电机轴）
#   KT_JOINT = rig.KT_JOINT（=MOTOR_PARAMS["kt"] × GEAR_RATIO ≈1.5825，关节侧）
from data_dog.go2_sim.rig import KT_JOINT  # noqa: E402
from data_dog.synth.generator import DEFAULT_PARAMS as _SYNTH_PARAMS  # noqa: E402

KT_SYNTH = _SYNTH_PARAMS["kt"]

# 各故障下的关节角速度（rad/s）。这份数据源没有动力学，只能按故障类型给约定值；
# 取值的依据是 go2_sim（MuJoCo）实测，见 `_emit` 说明第 2 条。
DQ_BY_FAULT: dict[str, float] = {
    "normal": 0.4,
    "F1_stall": 0.02,          # 堵转：关节被卡住，几乎不转
    "F2_temp_rise": 0.4,
    "F3_packet_loss": 0.4,
    "F4_current_bias": 0.4,
    "F5_load_spike": 0.4,
}
# 120 秒是 synth 数据集的既有口径（`FAULT_START_FRAC=0.4`，故障窗 48s~108s），
# 此前所有标定数字也按这个时长取。改短会让 F2 这类慢热故障来不及升温——那是
# 改了标尺，不是改了被测对象。
N_SECONDS = 120.0
JOINT = "joint_motor.front_left_hip"
WARMUP_SECONDS = 3.0     # 基线冷启动（Enricher 需 5 条才开放；30 帧够且不拖慢）
SEEDS = (0, 1, 2, 3, 4)
WINDOW_SIZE = 60         # 6 秒滑动窗口
AUTH = {"tenant_id": "tenant-real", "device_sn": "device-coverage-0001"}
T0 = datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc)


class _EventClock:
    """按**事件时间**推进的虚拟墙钟，给网关限流用。

    回放是把 60 秒数据一口气灌进去的，真实时钟只走了几毫秒；网关按墙钟令牌桶
    记账（200/s、burst 400），于是第 418 条之后全是 rate_limited——测出来的
    "全部无命中"其实是"根本没进检测器"。这是回放器的问题，不是限流器的问题，
    所以这里补时钟，不去调大生产限流阈值（那等于把被测系统改弱）。
    """

    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _raw(i: int, t_s: float, body: dict) -> dict:
    at = (T0 + timedelta(seconds=t_s)).isoformat().replace("+00:00", "Z")
    return {
        "event_id": f"e{i}", "occurred_at": at, "received_at": at, "sequence": i,
        "vendor": "unitree", "model": "go2", "firmware": "3.2.1",
        "protocol_version": "motor-log-v2", "component_hint": JOINT,
        "payload_inline": json.dumps(body),
    }


def _sample(fault: str | None, seed: int, seconds: float = N_SECONDS):
    cfg = SynthGenConfig(n_joints=1, joints=[JOINT], n_seconds=seconds, dt=DT,
                         seed=seed, active_fault=fault,
                         fault_component=JOINT if fault else "")
    return generate_sample(cfg)


def _make_plane():
    """建一个在线数据面，并给它的 Detector 挂**只读**的记录器。"""
    from data_dog.data_plane.gateway import TelemetryGateway

    rv = build_demo_resolver(_detector_config())
    clock = _EventClock(T0.timestamp() - WARMUP_SECONDS - 5.0)
    gw = TelemetryGateway(state_store=InMemoryStateStore())
    gw._clock = clock                  # 限流按事件时间记账，见 _EventClock 说明
    dp = DataPlane(rv, gateway=gw, window_size=WINDOW_SIZE)
    det = None
    for b in rv._bindings:                      # 只有一个绑定，取它的 detector
        det = b.extra.get("_detector")
    recorded: list[dict] = []
    original = det.detect

    def recording_detect(metrics, **kw):
        hits = original(metrics, **kw)
        series = kw.get("series") or {}
        samples = kw.get("samples") or 0
        recorded.append({
            "hits": [h["rule_id"] for h in hits],
            "subsystems": sorted({h.get("subsystem", "") for h in hits} - {""}),
            "metrics": dict(metrics),
            "coverage": (len(series.get("temperature") or []) / samples
                         if samples else None),
            "samples": samples,
        })
        return hits

    det.detect = recording_detect
    return dp, recorded, clock


def _emit(dp, clock, sample, t_off: float, fault: str | None = None) -> None:
    """把一条合成样本逐点喂进去；丢包点用"字段缺失"表达，不是填 0。

    ⚠ 两处**数据源与量纲的适配**，都是为了匹配检测侧已修正的单位制：

    1. **电流换算到关节侧 kt**。`synth/generator.py` 用的是电机轴 kt=0.25
       （`DEFAULT_PARAMS`），电流因此被放大 6.33 倍（实测 F1 堵转 95A）。
       检测规则已按关节侧 kt=1.5825 重写，直接喂旧电流会让**每一条**
       残差判据都错到十万八千里——实测 normal 也报 CURRENT_BIAS。
       这里用**保残差换算**（偏置是物理量，不随 kt 改变）：
           residual = I_old − τ/0.25        （τ 本来就是关节扭矩，不动）
           I_new    = τ/1.5825 + residual
       实测换算后残差：normal/F1/F2/F3/F5 恒 0.312，F4 恒 5.312 —— 正是
       规则要的分隔（门槛 4.0）。**不改 synth 本体**：它还被 visual_eval、
       mujoco_sim/conditions 消费，且 R/Rth/Cth 是配套 0.25 标定的。

    2. **dq 按故障给真实值**。原脚本把 dq 写死成 0.4，于是
       `MOTOR_STALL_SUSTAINED` 的"关节不转"判据永远不成立、F1 100% 漏检。
       堵转的物理定义就是关节被卡住，dq 必须是接近 0 的数。这份数据源
       没有真实动力学，只能按故障类型给约定值——这是**合成数据的边界**，
       不是判据的近似：取值来自 go2_sim（MuJoCo）实测（F1 堵转 mean|dq|≤0.008，
       其余姿态 0.02~2.79），这里取保守的 0.02 / 0.4。
    """
    dq = DQ_BY_FAULT.get(fault or "normal", DQ_BY_FAULT["normal"])
    for t in range(sample.n_steps):
        t_s = t_off + t * DT
        clock.now = T0.timestamp() + t_s          # 限流按事件时间记账
        cur, tem, tor = sample.current[t, 0], sample.temperature[t, 0], sample.torque[t, 0]
        missing = (cur != cur) or (tem != tem) or (tor != tor)
        body = {"mode": 0, "dq": dq, "q": 0.0, "lost": 0}
        if not missing:
            i_new = float(tor) / KT_JOINT + (float(cur) - float(tor) / KT_SYNTH)
            body.update(current=i_new, temperature=float(tem), tau_est=float(tor))
        dp.process_one(_raw(t, t_s, body), AUTH)


def probe(fault: str | None, seed: int) -> dict:
    dp, rec, clock = _make_plane()
    _emit(dp, clock, _sample(None, 1000 + seed, WARMUP_SECONDS), -WARMUP_SECONDS,
          fault=None)
    _emit(dp, clock, _sample(fault, seed), 0.0, fault=fault)

    # recorded 与"进入检测器的每一条"一一对应；故障窗 [40%,90%)
    warm = int(round(WARMUP_SECONDS / DT))
    lo = warm + int(N_SECONDS * 0.4 / DT)
    hi = warm + int(N_SECONDS * 0.9 / DT)
    seg = rec[lo:min(hi, len(rec))]
    counter: Counter = Counter()
    subs: Counter = Counter()
    silent = 0
    first_hit_at: float | None = None
    for i, r in enumerate(seg):
        for rule in set(r["hits"]):
            counter[rule] += 1
        for sb in r["subsystems"]:
            subs[sb] += 1
        if r["hits"]:
            if first_hit_at is None:
                first_hit_at = (lo + i) * DT - N_SECONDS * 0.4
        else:
            silent += 1
    last = seg[-1] if seg else {"hits": [], "metrics": {}, "coverage": None}
    return {
        "fault": fault or "normal", "seed": seed, "n_fault_events": len(seg),
        "rules_seen": dict(counter), "subsystems": dict(subs),
        "final_hits": sorted(last["hits"]),
        "no_hit_events": silent,
        # 从故障真实开始（40%）到**第一次**报出，隔了多久。这是运维真正关心的
        # 数字："能不能报"和"多久才报"是两件事。
        "first_hit_delay_s": (round(first_hit_at, 1)
                              if first_hit_at is not None else None),
        "last": {k: (round(v, 2) if isinstance(v, float) else v)
                 for k, v in last["metrics"].items() if not k.startswith("_")},
        "last_coverage": (round(last["coverage"], 3)
                          if last["coverage"] is not None else None),
    }


def main() -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--seeds", type=int, default=len(SEEDS))
    args = ap.parse_args()
    seeds = SEEDS[: args.seeds]

    rows = [probe(f, s) for f in (None, *FAULT_DEFS) for s in seeds]

    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r["fault"], {"rules": Counter(), "final": Counter(),
                                        "subs": Counter(),
                                        "seeds": 0, "events": 0, "silent": 0,
                                        "delays": [], "caught_seeds": 0})
        a["seeds"] += 1
        a["events"] += r["n_fault_events"]
        a["silent"] += r["no_hit_events"]
        a["rules"].update(r["rules_seen"])
        a["subs"].update(r["subsystems"])
        a["final"].update(r["final_hits"])
        if r["first_hit_delay_s"] is not None:
            a["caught_seeds"] += 1
            a["delays"].append(r["first_hit_delay_s"])

    if args.json:
        print(json.dumps({"rows": rows, "aggregate": {
            f: {"rules": dict(a["rules"]), "final": dict(a["final"]),
                "seeds": a["seeds"], "events": a["events"], "silent": a["silent"],
                "subsystems": dict(a["subs"]),
                "caught_seeds": a["caught_seeds"], "delays": a["delays"]}
            for f, a in agg.items()}}, ensure_ascii=False, indent=2))
        return 0

    print("=" * 96)
    print(f"在线链故障覆盖实测（DataPlane 全链，{DT=}，{N_SECONDS:g}s，"
          f"故障窗 40%~90%）")
    print("=" * 96)
    print(f"每类故障 {len(seeds)} 次独立运行；判定看命中规则，不只看有无报警\n")
    print(f"{'故障':<16}{'子系统':>6}{'运行中报出':>10}{'首次报出延迟':>13}"
          f"{'故障窗触发率':>12}  命中规则（事件数/末帧上报数）")
    print("-" * 96)
    for name, a in agg.items():
        n, silent = a["events"], a["silent"]
        rate = (n - silent) / n if n else 0.0
        ds = a["delays"]
        delay = f"{min(ds):.1f}~{max(ds):.1f}s" if len(ds) > 1 else (
            f"{ds[0]:.1f}s" if ds else "—")
        rules = ", ".join(
            f"{k}×{a['rules'][k]}/末{a['final'].get(k, 0)}"
            for k in sorted(a["rules"], key=lambda x: -a["rules"][x])) or "（无）"
        sub = "+".join(sorted(a["subs"])) or "—"
        print(f"{name:<16}{sub:>6}{a['caught_seeds']:>6}/{a['seeds']:<3}{delay:>13}"
              f"{rate:>12.1%}  {rules}")
    print("-" * 96)
    return 0


if __name__ == "__main__":
    sys.exit(main())
