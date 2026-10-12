"""视觉通道 A/B 评估（VISTA-1 式）：同一 Agent、同一批样本，只切"有 / 无视觉证据"。

为什么要有它
-----------
`evidence/visual.py` + Agent 视觉工具只是**机制**；"看了原始帧是否更准"必须实测。本脚本
把同一批合成样本跑两遍（关 / 开视觉），报 accuracy 与 **normal 误报率**，供同口径对照。

诚实边界
--------
- 样本是**合成遥测渲染出的图**（温升 / 电流曲线 + 阈值线），**不是真机相机画面**；
  MuJoCo 渲染 / Go2 WebRTC 接入是下一步。
- 图里**只画测量值与阈值，不画标签 / 故障名**（不泄答案）。
- 小样本（默认每类 4 条）→ Wilson CI 很宽；**只作方向参考，不当结论**。真实模型调用
  会花钱 / 耗时。
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from data_dog.evidence.visual import VisualFrameStore  # noqa: E402
from data_dog.providers.client import Provider  # noqa: E402
from data_dog.providers.config import load_config  # noqa: E402
from data_dog.synth import FAULT_DEFS, SynthGenConfig, synthesize_batch  # noqa: E402
from data_dog.synth.synth_eval import eval_one  # noqa: E402

JOINT = "joint_motor.front_left_hip"
WINDOW_START, WINDOW_END = "09:55", "10:00"


def render_frame(sample, joint: str, *, w: int = 480, h: int = 260) -> bytes:
    """把该关节的温升 / 电流画成一张图（只画测量值与阈值，**不画标签**）。"""
    from PIL import Image, ImageDraw
    idx = sample.joints.index(joint)
    temp = [float(v) for v in sample.temperature[:, idx]]
    cur = [float(v) for v in sample.current[:, idx]]
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)

    def panel(y0: int, y1: int, series: list, lo: float, hi: float, color, title: str,
              thresh=()) -> None:
        d.rectangle([40, y0, w - 10, y1], outline=(170, 170, 170))
        n = len(series)
        pts = []
        for i, v in enumerate(series):
            if v is None or (isinstance(v, float) and math.isnan(v)):
                continue
            x = 40 + (w - 50) * (i / max(1, n - 1))
            y = y1 - (y1 - y0) * ((v - lo) / (hi - lo))
            pts.append((x, min(max(y, y0), y1)))
        if len(pts) >= 2:
            d.line(pts, fill=color, width=2)
        for tv, tc in thresh:
            ty = y1 - (y1 - y0) * ((tv - lo) / (hi - lo))
            ty = min(max(ty, y0), y1)
            d.line([40, ty, w - 10, ty], fill=tc, width=1)
        d.text((3, y0), title, fill=(0, 0, 0))

    panel(10, 120, temp, 20, 110, (0, 80, 220), "T(C)",
          [(75, (230, 140, 0)), (90, (220, 0, 0))])
    panel(140, 250, cur, 0, 30, (0, 140, 0), "I(A)")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_samples(n_per_class: int, seed: int = 7):
    faults = ["normal", *FAULT_DEFS.keys()]
    cfgs, labels = [], []
    for fi, f in enumerate(faults):
        for k in range(n_per_class):
            cfgs.append(SynthGenConfig(
                n_joints=1, joints=[JOINT], n_seconds=120.0, dt=0.01,
                seed=seed * 1000 + fi * 100 + k,
                active_fault=None if f == "normal" else f,
                fault_component="" if f == "normal" else JOINT))
            labels.append({"id": f"{f}_{k}", "fault": f,
                           "component": "" if f == "normal" else JOINT,
                           "expected": "rejected" if f == "normal" else "needs_human_review"})
    return synthesize_batch(cfgs), labels


def summarize(results: list[dict]) -> dict:
    n = len(results)
    hit = sum(1 for r in results if r["hit"])
    normals = [r for r in results if r["fault"] == "normal"]
    fp = (sum(1 for r in normals if not r["hit"]) / len(normals)) if normals else 0.0
    by = {}
    for f in ["normal", *FAULT_DEFS.keys()]:
        g = [r for r in results if r["fault"] == f]
        if not g:
            continue
        by[f] = ({"n": len(g), "fp_rate": round(fp, 3)} if f == "normal"
                 else {"n": len(g), "recall": round(sum(1 for r in g if r["hit"]) / len(g), 3)})
    return {"n": n, "accuracy": round(hit / n, 4) if n else 0.0,
            "normal_fp_rate": round(fp, 3), "by_fault": by}


def run(samples, labels, stores, provider, model, workers, with_visual):
    def one(i: int) -> dict:
        trace: list = []
        r = eval_one(samples[i], labels[i], provider, model=model,
                     visual=(stores[i] if with_visual else None), trace_cb=trace.append)
        r["called_visual"] = any(str(t.get("name", "")).startswith(("query_visual", "view_visual"))
                                 for t in trace)
        return r
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(one, range(len(samples))))


def main() -> int:
    ap = argparse.ArgumentParser(description="视觉通道 A/B 评估（合成渲染图）")
    ap.add_argument("--n-per-class", type=int, default=4)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--visual", choices=("both", "on", "off"), default="both")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = load_config(_ROOT)
    if not cfg["api_key"]:
        print("需要先配 key（.env）", file=sys.stderr)
        return 1
    provider = Provider.from_config(cfg["vendor_id"], cfg["api_key"],
                                   base_url=cfg.get("base_url") or "")
    model = cfg["model"] or None

    samples, labels = build_samples(args.n_per_class)
    root = Path(tempfile.mkdtemp(prefix="dd_visual_"))
    stores = []
    for i, s in enumerate(samples):
        st = VisualFrameStore(root / f"vf{i}")
        fr = st.put(render_frame(s, s.joints[0]), device_sn="device-synth",
                    component_ref=s.joints[0], observed_at=WINDOW_START,
                    label=f"synth-{i}", source="synthetic-render")
        assert fr["frame_id"]
        stores.append(st)
    print(f"样本 {len(samples)} 条（每类 {args.n_per_class}）| 帧目录 {root}")

    out: dict = {"n_per_class": args.n_per_class, "model": model, "base_url": cfg["base_url"]}
    for cond in (("off", "on") if args.visual == "both" else (args.visual,)):
        res = run(samples, labels, stores, provider, model, args.workers, cond == "on")
        s = summarize(res)
        s["visual_tool_call_rate"] = (round(sum(1 for r in res if r["called_visual"]) / len(res), 3)
                                      if cond == "on" else 0.0)
        out[cond] = s
        print(f"[{cond:>3}] accuracy={s['accuracy']}  normal_FP={s['normal_fp_rate']}"
              f"  调了视觉工具={s['visual_tool_call_rate']}")
    if args.visual == "both":
        out["delta_accuracy"] = round(out["on"]["accuracy"] - out["off"]["accuracy"], 4)
        print(f"Δaccuracy(on-off) = {out['delta_accuracy']}")
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
