"""go2_sim（MuJoCo）端到端故障覆盖实测：与 `online_fault_coverage.py` 同一条在线链。

为什么要单独一份
----------------
`online_fault_coverage.py` 喂的是 `synth.generator`——纯扭矩/热模型，没有动力学，
`dq` 只能按故障类型约定。演示真正跑的是 go2_sim（MuJoCo 接触动力学），
它的 τ 有正有负、`dq` 是真实解算出来的。**规则在这两个数据源上的表现不同**，
必须分开测，否则"覆盖率"只是合成模型的覆盖率。

跑法：
    python scripts/go2_sim_coverage.py
    python scripts/go2_sim_coverage.py --json --seconds 40
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data_dog.core.console import ensure_utf8_console  # noqa: E402
from data_dog.data_plane.gateway import TelemetryGateway  # noqa: E402
from data_dog.data_plane.p2_demo import _detector_config  # noqa: E402
from data_dog.data_plane.pipeline import DataPlane, build_demo_resolver  # noqa: E402
from data_dog.go2_sim import Go2Sim, SimConfig  # noqa: E402
from data_dog.go2_sim.emit import envelope  # noqa: E402
from data_dog.state.memory import InMemoryStateStore  # noqa: E402

T0 = datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc)
AUTH = {"tenant_id": "tenant-real", "device_sn": "device-go2sim-0001"}
FAULTS = ("normal", "F1_stall", "F2_temp_rise", "F3_packet_loss",
          "F4_current_bias", "F5_load_spike", "F6_encoder_jump")
POSTURES = ("stand", "run", "prone")


class _Clock:
    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _plane():
    rv = build_demo_resolver(_detector_config())
    clock = _Clock(T0.timestamp() - 60.0)
    gw = TelemetryGateway(state_store=InMemoryStateStore())
    gw._clock = clock
    dp = DataPlane(rv, gateway=gw, window_size=60)
    det = None
    for b in rv._bindings:
        det = b.extra.get("_detector")
    rec: list[dict] = []
    original = det.detect

    def recording(metrics, **kw):
        hits = original(metrics, **kw)
        rec.append({
            "hits": [h["rule_id"] for h in hits],
            "baseline": dict(kw.get("baseline") or {}),
            "samples": kw.get("samples") or 0,
        })
        return hits

    det.detect = recording
    return dp, rec, clock


def probe(posture: str, fault: str, seconds: float, seed: int,
          fault_start_s: float = 5.0) -> dict:
    dp, rec, clock = _plane()
    sim = Go2Sim(SimConfig(posture=posture, fault=fault, severity=1.0, seed=seed,
                           fault_start_s=fault_start_s, thermal_scale=80.0))
    seq, nxt, t_index = 0, 0.0, 0
    try:
        for ch in sim.step(int(seconds * 100)):
            # 10Hz 抽稀：与 synth 侧同口径，也让窗口是真实秒数
            if ch.t < nxt:
                continue
            nxt = ch.t + 0.1
            e = envelope(ch, sequence=seq, session_start=T0)
            seq += 1
            if e is None:
                continue
            clock.now = T0.timestamp() + ch.t
            dp.process_one(e, AUTH)
            rec[-1]["t"] = ch.t
            t_index += 1
    finally:
        sim.close()

    # 故障窗 = [fault_start_s, seconds)；窗口/基线的热身前若干帧不纳入判定
    seg = [r for r in rec if r.get("t", 0.0) >= fault_start_s]
    counter: Counter = Counter()
    first_at = None
    for r in seg:
        for rule in set(r["hits"]):
            counter[rule] += 1
        if r["hits"] and first_at is None:
            first_at = r["t"] - fault_start_s
    last = seg[-1] if seg else {"hits": []}
    return {
        "posture": posture, "fault": fault, "seed": seed,
        "n": len(seg), "rules": dict(counter),
        "final": sorted(last["hits"]),
        "first_hit_delay_s": round(first_at, 1) if first_at is not None else None,
    }


def main() -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--seconds", type=float, default=40.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--postures", default="stand,run,prone")
    ap.add_argument("--faults", default=",".join(FAULTS))
    args = ap.parse_args()

    postures = [p for p in args.postures.split(",") if p]
    faults = [f for f in args.faults.split(",") if f]
    rows = [probe(p, f, args.seconds, args.seed) for p in postures for f in faults]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print("=" * 92)
    print(f"go2_sim（MuJoCo）端到端覆盖实测 — {args.seconds:g}s，seed={args.seed}，"
          f"故障自 5s 起注入")
    print("=" * 92)
    print(f"{'姿态':<7}{'故障':<15}{'末帧判定':<40}{'事件数':>8}  命中规则（事件数/末帧）")
    print("-" * 92)
    for r in rows:
        rules = ", ".join(
            f"{k}×{r['rules'][k]}/末{'✓' if k in r['final'] else '·'}"
            for k in sorted(r["rules"], key=lambda x: -r["rules"][x])) or "（无）"
        final = ",".join(r["final"]) or "—"
        print(f"{r['posture']:<7}{r['fault']:<15}{final:<40}{r['n']:>8}  {rules}")
    print("-" * 92)
    return 0


if __name__ == "__main__":
    sys.exit(main())
