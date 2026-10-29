"""go2_sim 全量故障矩阵：23 条 + normal，跑**真实** `_PlaneSink` 双数据面。

为什么要有这一份（和 `go2_sim_coverage.py` 的区别）
---------------------------------------------------
`go2_sim_coverage.py` 只装**电机**算子（`build_demo_resolver(_detector_config())`），
所以电池/散热/通信/整机那 11 类故障在它那里**结构上不可能出候选**——它测的是
"电机规则在 MuJoCo 数据上灵不灵"，不是"整套检测链路认得几类故障"。

演示真正跑的是 `server.py` 的 `_PlaneSink`：两条独立数据面，按信封
`component_hint` 分流，电机部件走电机算子、四个子系统部件走子系统算子。
那份矩阵以前是手工跑出来的（90 仿真秒、severity=1.0、thermal_scale=80、
posture=run），**没有脚本**，所以每次引用都得靠记忆，数字漂了也查不出来。

本脚本把那次手工测量固化下来：同一套 `_PlaneSink` 构造、同一套工况，
一条命令重跑全部 24 个情形。

跑法
----
    python scripts/go2_sim_matrix.py                    # 全量，90 仿真秒
    python scripts/go2_sim_matrix.py --seconds 20       # 快速冒烟
    python scripts/go2_sim_matrix.py --json > m.json

判读
----
- `首检(s)`：故障自**第 5 秒**注入（先留正常段给 EMA 基线），第一次出候选时距注入点
  多少秒。`None` = 整段没检出。
- `末帧`：最后一个样本仍在报的规则。窗口类判据中途报过、末帧掉了，说明
  信号被稀释，不能算稳定检出。
- `normal` 必须**零命中**，否则整张表作废（那是误报，不是覆盖）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_dog.core.console import ensure_utf8_console  # noqa: E402
from data_dog.go2_sim import Go2Sim, SimConfig  # noqa: E402
from data_dog.go2_sim.emit import EPOCH, envelope, subsystem_envelopes  # noqa: E402
from data_dog.go2_sim.faults import FAULT_LIBRARY  # noqa: E402
from data_dog.go2_sim.server import WINDOW_SIZE, _PlaneSink  # noqa: E402
from data_dog.go2_sim.session import SimEvent  # noqa: E402

PUBLISH_HZ = 10.0
# doc 58 A 类表的原始工况：90 仿真秒、severity=1.0、thermal_scale=80、run
DEFAULT_SECONDS = 90.0
DEFAULT_THERMAL_SCALE = 80.0
DEFAULT_POSTURE = "run"
# 故障从**第 5 秒**才注入，不是 t=0。
#
# **t=0 注入会把整张表测废。** 基线是 EMA，它必须只反映"故障之前"的历史
# （`pipeline.process_one` 的注释说得很明白：先取基线、再并入本样本）。
# 若第一帧就是故障帧，基线本身就是故障值，所有"相对基线"的判据——
# `baseline_abs(τ)`、`mean(I) − mean_abs(τ)/kt`、温度斜率——全部失去参照。
# 实测：`fault_start_s=0` 时 F1 堵转一整段 0 命中，而卯上 5 秒正常段后立刻能报。
DEFAULT_FAULT_START_S = 5.0


def _instrument(sink: _PlaneSink) -> list[dict]:
    """挂记录器：每个候选构建都留下 (部件, 规则集, 时刻)。

    挂在 `builder.add` 而不是 `detector.detect` —— `detect` 不知道自己在为哪个
    部件求值（`process_one` 只把 metrics 传进去），而 `add` 的入参里
    `component_ref` 和 `hits` 都是现成的。这条路径也正是真实产出候选的路径。
    """
    rec: list[dict] = []
    for label, plane, _comps in sink._planes:
        # 默认参数把 label/original 绑死在**这一轮**，否则闭包晚绑定，
        # 每条数据面的记录都会带上最后一次循环的 label（实测过，静默记错）
        def wrapped(*a, _label=label, _orig=plane.builder.add, **kw):
            hits = kw.get("hits") or []
            rec.append({
                "plane": _label,
                "component": kw.get("component_ref"),
                "rules": [h.get("rule_id") for h in hits],
                "at": kw.get("occurred_at"),
            })
            return _orig(*a, **kw)

        plane.builder.add = wrapped
    return rec


def _bind_gateway_clock(sink: _PlaneSink, start_epoch: float) -> _SimClock:
    """把两条数据面的网关时钟接到**仿真钟**上。

    **不接就会误报。** 网关限流按它自己的 `_clock` 算（默认墙钟），而本脚本
    全速跑仿真：90 仿真秒 50 条/s = 4500 条，实际两三十墙钟秒就灌完了，
    折算 150+ 条/s 逼近 200 条/s 上限，偶尔超了就拒。被拒的信封在窗口里
    是**序列号空洞**，`COMM_PACKET_COVERAGE` 于是把限流误判成丢包。
    实测冒烟跑 `normal` 就出了 5 帧 `COMM_PACKET_LOSS`——不是检测器的问题，
    是测量装置的问题。

    `go2_sim_coverage.py` 早就这么做了（那里的 `_Clock`），本脚本一开始漏了。
    """
    clock = _SimClock(start_epoch)
    for _label, plane, _comps in sink._planes:
        plane.gateway._clock = clock
    return clock


class _SimClock:
    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def probe(posture: str, fault: str, seconds: float, seed: int,
          thermal_scale: float, severity: float,
          fault_start_s: float = DEFAULT_FAULT_START_S,
          window_size: int | None = None) -> dict:
    sink = _PlaneSink()
    if not sink._planes:
        raise RuntimeError(f"数据面建不起来：{sink.error}")
    if window_size is not None:
        from data_dog.data_plane.chain import WindowBuffer
        for _l, plane, _c in sink._planes:
            plane.window = WindowBuffer(size=window_size)
    rec = _instrument(sink)
    plane_err = sink.error
    clock = _bind_gateway_clock(sink, EPOCH.timestamp() - 60.0)

    sim = Go2Sim(SimConfig(posture=posture, fault=fault, severity=severity,
                           seed=seed, fault_start_s=fault_start_s,
                           thermal_scale=thermal_scale))
    seq = 0
    nxt = 0.0
    n_calls = 0
    t_end = 0.0
    try:
        for ch in sim.step(int(seconds * 100)):
            if ch.t < nxt:
                continue
            nxt = ch.t + 1.0 / PUBLISH_HZ
            # 网关时钟跟着仿真钟走，否则全速跑会被限流拒成假丢包（见 _bind_gateway_clock）
            clock.now = EPOCH.timestamp() + float(ch.t)
            # 一个投递点 = 5 条信封（1 电机 + 4 子系统），与 session._publish 同序
            batch = []
            e = envelope(ch, sequence=seq)
            if e is not None:
                batch.append(e)
            batch += subsystem_envelopes(ch, sequence=seq)
            for one in batch:
                sink.push(SimEvent(t=float(ch.t), env=one))
            n_calls += 1
            t_end = float(ch.t)
            seq += 1    # 电机信封发不出时序列号也要前进，形成空洞（=丢包语义）
    finally:
        sim.close()

    # 全部候选落盘后再统计：最后那段窗口的命中要等 _collect()
    sink._collect()

    after = [r for r in rec if _sim_t(r["at"]) >= fault_start_s]
    counter: dict[str, int] = {}
    for r in after:
        for rule in set(r["rules"]):
            counter[rule] = counter.get(rule, 0) + 1
    first = None
    for r in sorted(after, key=lambda x: str(x["at"])):
        if r["rules"]:
            first = round(_sim_t(r["at"]) - fault_start_s, 1)
            break
    # 末帧：最后一次**有命中**的那条记录里的规则集
    last_rules: list[str] = []
    for r in sorted(after, key=lambda x: str(x["at"])):
        if r["rules"]:
            last_rules = sorted(set(r["rules"]))

    return {
        "posture": posture, "fault": fault, "seed": seed,
        "seconds": seconds, "severity": severity,
        "thermal_scale": thermal_scale,
        "published": n_calls, "t_end": round(t_end, 1),
        "plane_error": plane_err,
        "hits": len(after), "rules": counter,
        "first_hit_s": first, "final_rules": last_rules,
        "components": sorted({r["component"] for r in after if r["rules"]}),
    }


def _sim_t(occurred_at) -> float:
    """occurred_at（ISO）→ 仿真秒。用信封自身的 EPOCH 基准反推。

    `emit.envelope` 用 `session_start + t`，默认 `EPOCH`。这里减一下即可，
    不引第二个时间源——否则"检出时刻"会和仿真时钟各算各的。
    """
    if not occurred_at:
        return 0.0
    from datetime import datetime, timezone
    s = str(occurred_at).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() - EPOCH.timestamp()


def main() -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--severity", type=float, default=1.0)
    ap.add_argument("--thermal-scale", type=float, default=DEFAULT_THERMAL_SCALE)
    ap.add_argument("--posture", default=DEFAULT_POSTURE)
    ap.add_argument("--window", type=int, default=None,
                    help=f"窗口样本数；不给则用 server.WINDOW_SIZE={WINDOW_SIZE}"
                         "（10Hz 下 6 秒，堵转规则的前置要 >=5 秒）")
    ap.add_argument("--faults", default="",
                    help="逗号分隔；默认 normal + 全部 23 条")
    args = ap.parse_args()

    # `FAULT_LIBRARY` 第一条就是 normal（对照项），不要重复加
    faults = ([f for f in args.faults.split(",") if f]
              if args.faults else [f.key for f in FAULT_LIBRARY])

    rows = []
    t0 = time.monotonic()
    for i, f in enumerate(faults, 1):
        started = time.monotonic()
        row = probe(args.posture, f, args.seconds, args.seed,
                    args.thermal_scale, args.severity,
                    window_size=args.window)
        row["wall_s"] = round(time.monotonic() - started, 1)
        rows.append(row)
        if not args.json:
            print(f"[{i}/{len(faults)}] {f:<22} "
                  f"首检={row['first_hit_s']}  命中帧={row['hits']}  "
                  f"{row['wall_s']}s", flush=True)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print("=" * 100)
    print(f"go2_sim 全量矩阵 — posture={args.posture}  {args.seconds:g} 仿真秒  "
          f"seed={args.seed}  severity={args.severity:g}  "
          f"thermal_scale={args.thermal_scale:g}  投递 {PUBLISH_HZ:g}Hz  "
          f"窗口={args.window or WINDOW_SIZE} 样本")
    print(f"总墙钟 {time.monotonic() - t0:.1f}s")
    print("=" * 100)
    print(f"{'故障':<22}{'首检(s)':>9}{'命中帧':>7}  规则（帧数）")
    print("-" * 100)
    detected = 0
    for r in rows:
        if r["fault"] != "normal" and r["hits"]:
            detected += 1
        rules = ", ".join(f"{k}×{v}" for k, v in
                          sorted(r["rules"].items(), key=lambda x: -x[1])) or "（无）"
        print(f"{r['fault']:<22}{str(r['first_hit_s']):>9}{r['hits']:>7}  {rules}")
    print("-" * 100)
    normal = next((r for r in rows if r["fault"] == "normal"), None)
    print(f"检出 {detected}/{len(faults) - 1} 条；"
          f"normal 命中帧 = {normal['hits'] if normal else 'n/a'} "
          f"{'✅' if normal and normal['hits'] == 0 else '❌ 误报'}")
    # 慢热的那几条按自己的时间常数越线，跑短了会假性漏检——如实标注，
    # 别让"17/23"被当成覆盖率
    slow = [r["fault"] for r in rows
            if r["fault"] != "normal" and not r["hits"]
            and r["fault"] in ("F13_battery_hot",)]
    if slow:
        print(f"⚠ {args.seconds:g} 仿真秒不足以让 {', '.join(slow)} 越线"
              f"（电池热惯性，实测需约 28 秒）。这不是漏检，默认 90 秒可检出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
