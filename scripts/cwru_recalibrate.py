"""CWRU 判据**重标定**：按载荷切"标定集 → 测试集"，看能不能补回那 ~28% 漏检。

为什么要按载荷切
---------------
同一条信号切出的窗口**高度相关**；随机切训练/测试 = **数据泄漏**（CWRU 文献里那些 99%+ 多半
就是这么来的）。按**载荷**切才是"换个工况还灵不灵"。

做法
- 候选判据：**单特征阈值规则**（在标定集上扫阈值，取平衡准确率最高）——刻意保持**确定性、
  可解释**（与本仓"热路径是阈值算子"的口径一致），不上黑箱。
- 特征：时域（rms / peak / kurtosis / crest_factor）+ **包络谱缺陷频率占比**
  （env_bpfo / env_bpfi / env_bsf）。
- 对照：**旧阈值**（借用合成档：kurtosis>4 或 crest>5） vs **重标定阈值**，都只在**测试载荷**上评。
- `--lolo`：**留一载荷交叉验证**——每个载荷轮流当测试集、其余当标定集，再聚合。单折容易骗人，
  多折才看得出"到底是标定对了，还是恰好那一折好"。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

from cwru_eval import hit, load_windows, summarize  # noqa: E402

from data_dog.core.console import ensure_utf8_console  # noqa: E402
from data_dog.evidence.cwru import (  # noqa: E402
    LOAD_RPM,
    envelope_features,
    features,
)


def featurize(label: str, load: int, x: np.ndarray) -> dict:
    fr = LOAD_RPM.get(load, 1797.0) / 60.0
    f = {**features(x), **envelope_features(x, fs=12000.0, fr=fr)}
    f["__label__"], f["__load__"] = label, load
    return f


def _bal_acc(rows: list[dict], feat: str, thr: float) -> tuple[float, float, float]:
    """以 "feat > thr" 判异常，返回 (平衡准确率, 故障召回, normal 误报率)。"""
    tp = sum(1 for r in rows if r["__label__"] != "normal" and r[feat] > thr)
    fn = sum(1 for r in rows if r["__label__"] != "normal" and r[feat] <= thr)
    fp = sum(1 for r in rows if r["__label__"] == "normal" and r[feat] > thr)
    tn = sum(1 for r in rows if r["__label__"] == "normal" and r[feat] <= thr)
    tpr = tp / (tp + fn) if (tp + fn) else 0.0
    tnr = tn / (tn + fp) if (tn + fp) else 0.0
    return (tpr + tnr) / 2.0, tpr, (fp / (fp + tn) if (fp + tn) else 0.0)


# 尺度不变特征：不随录音增益 / 量级漂移——跨工况迁移应优先用它们。
INVARIANT = {"kurtosis", "crest_factor", "env_bpfo", "env_bpfi", "env_bsf", "env_peak_hz"}


def fit_rule(calib: list[dict], allow: set[str] | None = None
             ) -> tuple[str, float, list[tuple[float, str, float]]]:
    """在标定集上为每个特征扫阈值（分位数），返回最优 (feature, thr, 全部打分)。

    allow 非空时只在给定特征集合里搜（用于"只用尺度不变特征"的对照）。
    """
    feats = [k for k in calib[0]
             if not k.startswith("__") and (allow is None or k in allow)]
    scored: list[tuple[float, str, float]] = []
    for f in feats:
        vals = sorted(r[f] for r in calib)
        qs = [vals[int(q * (len(vals) - 1))] for q in np.linspace(0.05, 0.95, 46)]
        ba, thr = max(((_bal_acc(calib, f, t)[0], t) for t in qs), key=lambda z: z[0])
        scored.append((ba, f, thr))
    scored.sort(reverse=True)
    return scored[0][1], scored[0][2], scored


def eval_rule(rows: list[dict], feat: str, thr: float) -> list[dict]:
    return [{"label": r["__label__"],
             "hit": hit(r["__label__"], "needs_human_review" if r[feat] > thr else "rejected")}
            for r in rows]


def baseline_rows(rows: list[dict]) -> list[dict]:
    return [{"label": r["__label__"],
             "hit": hit(r["__label__"],
                        "needs_human_review"
                        if (r["kurtosis"] > 4.0 or r["crest_factor"] > 5.0) else "rejected")}
            for r in rows]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CWRU 判据重标定（按载荷 held-out / LOLO）")
    ap.add_argument("--data-dir", default=str(_ROOT / "data" / "cwru"))
    ap.add_argument("--calib-load", type=int, default=0)
    ap.add_argument("--test-load", type=int, default=1)
    ap.add_argument("--lolo", action="store_true", help="留一载荷交叉验证（每个载荷轮流当测试集）")
    ap.add_argument("--invariant", action="store_true",
                    help="只用**尺度不变**特征（kurtosis/crest/env_*），排除 rms/peak 这类带量纲量")
    ap.add_argument("--windows-per-file", type=int, default=4)
    ap.add_argument("--window-len", type=int, default=24000)
    ap.add_argument("--stride", type=int, default=12000,
                    help="步长；=window-len 即**不重叠**窗口（减少窗口间相关性）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    ensure_utf8_console()

    wins = load_windows(Path(args.data_dir), args.windows_per_file,
                        args.window_len, args.stride)
    rows_all = [featurize(lb, ld, x) for lb, ld, x in wins]
    loads = sorted({r["__load__"] for r in rows_all})
    if len(loads) < 2:
        print(f"至少需要 2 个载荷，当前 {loads}", file=sys.stderr)
        raise SystemExit(1)

    folds: list[dict] = []
    agg_base: list[dict] = []
    agg_new: list[dict] = []
    combos = [None] if not args.lolo else loads
    for test_load in (combos if args.lolo else [args.test_load]):
        if args.lolo:
            calib = [r for r in rows_all if r["__load__"] != test_load]
            test = [r for r in rows_all if r["__load__"] == test_load]
        else:
            calib = [r for r in rows_all if r["__load__"] == args.calib_load]
            test = [r for r in rows_all if r["__load__"] == args.test_load]
        if not calib or not test:
            continue
        best_f, best_t, scored = fit_rule(calib, INVARIANT if args.invariant else None)
        b = summarize(baseline_rows(test))
        n = summarize(eval_rule(test, best_f, best_t))
        agg_base += baseline_rows(test)
        agg_new += eval_rule(test, best_f, best_t)
        folds.append({"test_load": test_load, "calib_n": len(calib), "test_n": len(test),
                      "rule": {"feature": best_f, "threshold": round(best_t, 5)},
                      "baseline": b, "recalibrated": n,
                      "top3": [{"feature": f, "threshold": round(t, 5), "calib_bal_acc": round(ba, 3)}
                               for ba, f, t in scored[:3]]})
        n_norm = sum(1 for r in test if r["__label__"] == "normal")
        print(f"fold test=op_{test_load} (calib {len(calib)} / test {len(test)} 窗口, "
              f"normal {n_norm})  规则 {best_f}>{best_t:.4g}  "
              f"旧 acc={b['accuracy']}/召回={b['fault_recall']}/FP={b['normal_fp_rate']}  "
              f"新 acc={n['accuracy']}/召回={n['fault_recall']}/FP={n['normal_fp_rate']}")

    out = {"loads": loads, "windows_per_file": args.windows_per_file, "folds": folds}
    if args.lolo:
        out["aggregate"] = {"baseline": summarize(agg_base), "recalibrated": summarize(agg_new),
                            "base_n": len(agg_base)}
        print(f"\n[聚合] 旧阈值   accuracy={out['aggregate']['baseline']['accuracy']}  "
              f"召回={out['aggregate']['baseline']['fault_recall']}  "
              f"normal误报={out['aggregate']['baseline']['normal_fp_rate']}")
        print(f"[聚合] 重标定   accuracy={out['aggregate']['recalibrated']['accuracy']}  "
              f"召回={out['aggregate']['recalibrated']['fault_recall']}  "
              f"normal误报={out['aggregate']['recalibrated']['normal_fp_rate']}")
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
