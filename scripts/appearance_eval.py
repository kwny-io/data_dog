"""外观故障 A/B：**遥测全正常**，只有画面里有故障 —— 视觉该有作用的场景。

为什么这样设计
-------------
上一版视觉 A/B（把遥测画成曲线）是负结果，因为图与文本**同源**。这里反过来：
- 遥测：**正常**（文本证据只能说"一切正常"）；
- 画面：MuJoCo 渲染的 Go2，叠加**外观故障**（漏油 / 形变 / 异物）或干净；
- ground truth：有外观故障 → `needs_human_review`；干净 → `rejected`。

于是"关视觉"几乎必然漏掉全部故障（文本看不到），"开视觉"才有机会——这才是在量
**视觉是否带来文本拿不到的信息**。

口径与边界
---------
- 外观故障是**画面叠加**（PIL），非物理仿真；但类别与 ground truth 明确。
- 样本量默认小（每类 3），CI 很宽：只作方向判断。
- 记录 `visual_tool_call_rate`：没调视觉工具的样本不算"干净对照"，单独报。
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

from render_go2_frames import DEFECTS, render_go2  # noqa: E402

from data_dog.agent.loop import AgentLoop  # noqa: E402
from data_dog.core.console import ensure_utf8_console  # noqa: E402
from data_dog.evidence.visual import VisualFrameStore  # noqa: E402
from data_dog.providers.client import Provider  # noqa: E402
from data_dog.providers.config import load_config  # noqa: E402
from data_dog.schemas.events import CandidateEvent  # noqa: E402
from data_dog.synth import SynthGenConfig, synthesize_batch  # noqa: E402
from data_dog.synth.evidence_source import SyntheticEvidenceSource  # noqa: E402
from data_dog.tools.investigation import InvestigationTools  # noqa: E402

JOINT = "joint_motor.front_left_hip"
WINDOW = "09:55"
_FLAGGED = ("needs_human_review", "confirmed")


def _normal_sample():
    cfg = SynthGenConfig(n_joints=1, joints=[JOINT], n_seconds=120.0, dt=0.01,
                         seed=11, active_fault=None, fault_component="")
    return synthesize_batch([cfg])[0]


def _cand(idx: int) -> CandidateEvent:
    # ⚠️ candidate_id 必须**中性**：把 defect 名写进 id 等于把答案递给模型（污染 A/B）。
    return CandidateEvent(candidate_id=f"cand_appr_{idx:03d}",
                          device_sn="device-synth", component_ref=JOINT,
                          signal="visual_inspection", severity_hint="warning",
                          window_start=WINDOW, window_end="10:00", sample_count=300,
                          rule_matches=["VISUAL_CHECK"], anomaly_score=0.5,
                          detector_provenance="appearance-check")


def _one(sample, cand, defect, visual, provider, model) -> dict:
    trace: list = []
    tools = InvestigationTools(SyntheticEvidenceSource([sample]), visual=visual)
    rep = AgentLoop(provider, tools, model=model, trace_cb=trace.append).investigate(cand)
    called = any(str(t.get("name", "")).startswith(("query_visual", "view_visual"))
                 for t in trace)
    expected_defect = defect is not None
    hit = (rep.disposition in _FLAGGED) if expected_defect else (rep.disposition == "rejected")
    return {"defect": defect or "clean", "expected": "defect" if expected_defect else "clean",
            "disposition": rep.disposition, "hit": hit, "called_visual": called}


def _run(samples, cands, defects, stores, visual_on, provider, model, workers):
    def go(i: int) -> dict:
        visual = stores[i] if visual_on else None
        return _one(samples[i], cands[i], defects[i], visual, provider, model)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(go, range(len(samples))))


def _summary(rows: list[dict]) -> dict:
    n = len(rows)
    defects = [r for r in rows if r["expected"] == "defect"]
    cleans = [r for r in rows if r["expected"] == "clean"]
    recall = sum(1 for r in defects if r["hit"]) / len(defects) if defects else 0.0
    fp = sum(1 for r in cleans if not r["hit"]) / len(cleans) if cleans else 0.0
    return {"n": n, "accuracy": round(sum(1 for r in rows if r["hit"]) / n, 4) if n else 0.0,
            "defect_recall": round(recall, 3), "clean_fp_rate": round(fp, 3),
            "visual_tool_call_rate": round(sum(1 for r in rows if r["called_visual"]) / n, 3) if n else 0.0}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="外观故障 A/B（遥测正常、画面含故障）")
    ap.add_argument("--n-per-class", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--visual", choices=("both", "on", "off"), default="both")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    ensure_utf8_console()

    cfg = load_config(_ROOT)
    if not cfg["api_key"]:
        print("需要先配 key（.env）", file=sys.stderr)
        raise SystemExit(1)
    provider = Provider.from_config(cfg["vendor_id"], cfg["api_key"],
                                    base_url=cfg.get("base_url") or "")
    model = cfg["model"] or None

    sample = _normal_sample()
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="dd_appearance_"))
    samples, cands, defects, stores = [], [], [], []
    for defect in DEFECTS:
        for i in range(args.n_per_class):
            cand = _cand(len(samples))
            st = VisualFrameStore(root / f"{defect or 'clean'}_{i}")
            st.put(render_go2(defect=defect, seed=i), device_sn="device-synth",
                   component_ref=JOINT, observed_at=WINDOW,
                   label=defect or "clean", source="mujoco-render")
            samples.append(sample)
            cands.append(cand)
            defects.append(defect)
            stores.append(st)
    print(f"样本 {len(samples)}（{len(DEFECTS)} 类 × {args.n_per_class}）| model={model} | frames={root}")

    out = {"n_per_class": args.n_per_class, "model": model}
    for cond in (("off", "on") if args.visual == "both" else (args.visual,)):
        rows = _run(samples, cands, defects, stores, cond == "on",
                    provider, model, args.workers)
        s = _summary(rows)
        out[cond] = s
        print(f"[{cond:>3}] accuracy={s['accuracy']}  故障召回={s['defect_recall']}  "
              f"干净误报={s['clean_fp_rate']}  调了视觉工具={s['visual_tool_call_rate']}")
    if args.visual == "both":
        out["delta_recall"] = round(out["on"]["defect_recall"] - out["off"]["defect_recall"], 3)
        print(f"Δ故障召回(on-off) = {out['delta_recall']}")
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
