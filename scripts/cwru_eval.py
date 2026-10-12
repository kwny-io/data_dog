"""跨数据集泛化：把 **CWRU 轴承公开数据集**接进 Agent，看"换个数据源照样用"是否成立。

为什么
-----
此前所有评估都在**自己造的合成 / MuJoCo 数据**上。工业界第一问是"你只对自己造的数据有效吧？"。
本脚本把 CWRU（公开、带故障标签）接成 `EvidenceSource`——Agent / 工具链**一行不改**——报两栏：
① **确定性阈值基线**；② **真 LLM Agent**。同一批窗口、同一标签口径。

口径与边界
---------
- 标签**粗粒度**：normal → rejected；ball / inner / outer → needs_human_review。
- 同一条信号切出的窗口**高度相关**：n 是窗口数，不是独立试验数，CI 要打折。
- 阈值（kurtosis > 4 / crest > 5）是从**合成档借来的、未在 CWRU 上标定**——基线差是预期内的；
  这正是"跨数据集需要重新标定"的证据，不是 bug。
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from data_dog.agent.loop import AgentLoop  # noqa: E402
from data_dog.context import RuntimeContext  # noqa: E402
from data_dog.core.console import ensure_utf8_console  # noqa: E402
from data_dog.evidence.cwru import (  # noqa: E402
    BEARING_COMPONENT,
    CWRUEvidenceSource,
    features,
    read_mat_variables,
)
from data_dog.providers.client import Provider  # noqa: E402
from data_dog.providers.config import load_config  # noqa: E402
from data_dog.schemas.events import CandidateEvent  # noqa: E402
from data_dog.tools.investigation import InvestigationTools  # noqa: E402

# 数据文件 → 标签（data/ 已 gitignore；镜像仓库见 docs/agent_cost_latency.md 同源说明）
FILES: dict[str, str] = {
    "op_0/normal/97.mat": "normal",
    "op_0/ball_07/118.mat": "ball",
    "op_0/ball_14/185.mat": "ball",
    "op_0/ball_21/222.mat": "ball",
    "op_0/inner_07/105.mat": "inner",
    "op_0/inner_14/169.mat": "inner",
    "op_0/outer_07/130.mat": "outer",
    "op_0/outer_14/197.mat": "outer",
    "op_0/outer_21/234.mat": "outer",
}
_FLAGGED = ("needs_human_review", "confirmed")


def _load_windows(data_dir: Path, per_file: int, wlen: int, stride: int):
    """读每个文件的 DE 信号并切窗；返回 [(label, window), ...]。"""
    out = []
    for rel, label in FILES.items():
        p = data_dir / rel
        if not p.exists():
            continue
        try:
            vars_ = read_mat_variables(p)
        except Exception as e:                         # noqa: BLE001
            print(f"[warn] {rel}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        key = next((k for k in vars_ if "DE" in k), None)
        if key is None:
            print(f"[warn] {rel}: 无 DE 变量（有 {list(vars_)}）", file=sys.stderr)
            continue
        x = vars_[key]
        n = 0
        for start in range(0, max(0, len(x) - wlen), stride):
            if n >= per_file:
                break
            out.append((label, x[start:start + wlen]))
            n += 1
    return out


def _cand(i: int) -> CandidateEvent:
    # candidate_id 中性：不许把标签写进 id（评测污染）。
    return CandidateEvent(candidate_id=f"cand_cwru_{i:03d}", device_sn="device-cwru",
                          component_ref=BEARING_COMPONENT, signal="vibration_anomaly",
                          severity_hint="warning", window_start="w0", window_end="w0",
                          sample_count=24000, rule_matches=["CWRU_VIB"],
                          anomaly_score=0.5, detector_provenance="cwru")


def _hit(label: str, disposition: str) -> bool:
    return (disposition in _FLAGGED) if label != "normal" else (disposition == "rejected")


def _baseline(label: str, x: np.ndarray) -> bool:
    f = features(x)
    anom = f.get("kurtosis", 0.0) > 4.0 or f.get("crest_factor", 0.0) > 5.0
    return _hit(label, "needs_human_review" if anom else "rejected")


def _summary(rows: list[dict]) -> dict:
    n = len(rows)
    faults = [r for r in rows if r["label"] != "normal"]
    normals = [r for r in rows if r["label"] == "normal"]
    return {"n": n,
            "accuracy": round(sum(1 for r in rows if r["hit"]) / n, 4) if n else 0.0,
            "fault_recall": round(sum(1 for r in faults if r["hit"]) / len(faults), 3) if faults else 0.0,
            "normal_fp_rate": round(sum(1 for r in normals if not r["hit"]) / len(normals), 3) if normals else 0.0,
            "by_fault": {lb: sum(1 for r in faults if r["label"] == lb and r["hit"])
                         for lb in sorted({r["label"] for r in faults})}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CWRU 跨数据集评估")
    ap.add_argument("--data-dir", default=str(_ROOT / "data" / "cwru"))
    ap.add_argument("--windows-per-file", type=int, default=4)
    ap.add_argument("--window-len", type=int, default=24000)
    ap.add_argument("--stride", type=int, default=12000)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--baseline-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    ensure_utf8_console()

    wins = _load_windows(Path(args.data_dir), args.windows_per_file, args.window_len, args.stride)
    if not wins:
        print("没有可用窗口：先下载 data/cwru（见脚本内 FILES 清单）", file=sys.stderr)
        raise SystemExit(1)
    labels = sorted({lbl for lbl, _ in wins})
    counts = {lb: sum(1 for lbl, _ in wins if lbl == lb) for lb in labels}
    print(f"窗口 {len(wins)} 条（每文件 {args.windows_per_file}）| 类别 {counts}")

    base = [{"label": lb, "hit": _baseline(lb, x)} for lb, x in wins]
    out = {"n": len(wins), "baseline": _summary(base)}
    print(f"[baseline] accuracy={out['baseline']['accuracy']}  "
          f"故障召回={out['baseline']['fault_recall']}  normal误报={out['baseline']['normal_fp_rate']}")

    if not args.baseline_only:
        cfg = load_config(_ROOT)
        if not cfg["api_key"]:
            print("需要 key 才能跑 LLM 栏", file=sys.stderr)
            raise SystemExit(1)
        provider = Provider.from_config(cfg["vendor_id"], cfg["api_key"],
                                        base_url=cfg.get("base_url") or "")
        model = cfg["model"] or None
        ctx = RuntimeContext(tenant_id="cwru-eval", agent_id="cwru-eval",
                             device_sn="device-cwru",
                             allowed_components=frozenset({BEARING_COMPONENT}))

        def one(i: int) -> dict:
            label, x = wins[i]
            tools = InvestigationTools(CWRUEvidenceSource(x), ctx=ctx)
            rep = AgentLoop(provider, tools, model=model).investigate(_cand(i))
            return {"label": label, "hit": _hit(label, rep.disposition),
                    "disposition": rep.disposition}

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            rows = list(ex.map(one, range(len(wins))))
        out["llm"] = _summary(rows)
        print(f"[llm]      accuracy={out['llm']['accuracy']}  "
              f"故障召回={out['llm']['fault_recall']}  normal误报={out['llm']['normal_fp_rate']}")
        print(f"  分类命中: {out['llm']['by_fault']}")
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
