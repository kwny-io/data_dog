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
- 对照：**旧阈值**（借用合成档：kurtosis>4 或 crest>5） vs **重标定阈值**，都在**测试载荷**上评。
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


def _rows_with_hit(rows: list[dict], feat: str, thr: float) -> list[dict]:
    return [{"label": r["__label__"],
             "hit": hit(r["__label__"], "needs_human_review" if r[feat] > thr else "rejected")}
            for r in rows]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CWRU 判据重标定（按载荷 held-out）")
    ap.add_argument("--data-dir", default=str(_ROOT / "data" / "cwru"))
    ap.add_argument("--calib-load", type=int, default=0)
    ap.add_argument("--test-load", type=int, default=1)
    ap.add_argument("--windows-per-file", type=int, default=4)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    ensure_utf8_console()

    wins = load_windows(Path(args.data_dir), args.windows_per_file, 24000, 12000)
    calib = [featurize(lb, ld, x) for lb, ld, x in wins if ld == args.calib_load]
    test = [featurize(lb, ld, x) for lb, ld, x in wins if ld == args.test_load]
    if not calib or not test:
        print(f"缺少载荷数据：calib(op_{args.calib_load})={len(calib)} test(op_{args.test_load})={len(test)}",
              file=sys.stderr)
        raise SystemExit(1)
    feats = [k for k in calib[0] if not k.startswith("__")]
    print(f"标定 op_{args.calib_load}: {len(calib)} 窗口 | 测试 op_{args.test_load}: {len(test)} 窗口")

    # 标定：每个特征扫阈值，取平衡准确率最高
    scored = []
    for f in feats:
        vals = sorted(r[f] for r in calib)
        qs = [vals[int(q * (len(vals) - 1))] for q in np.linspace(0.05, 0.95, 46)]
        best = max(((_bal_acc(calib, f, t)[0], t) for t in qs), key=lambda z: z[0])
        scored.append((best[0], f, best[1]))
    scored.sort(reverse=True)
    print("标定集 top5 规则：")
    for ba, f, t in scored[:5]:
        print(f"  {f} > {t:.4g}   bal_acc={ba:.3f}")

    best_ba, best_f, best_t = scored[0]

    # 基线要用原始信号（kurtosis/crest 已在特征里，等价）
    base_rows = [{"label": r["__label__"],
                  "hit": hit(r["__label__"],
                             "needs_human_review"
                             if (r["kurtosis"] > 4.0 or r["crest_factor"] > 5.0) else "rejected")}
                 for r in test]
    new_rows = _rows_with_hit(test, best_f, best_t)
    out = {
        "calib_load": args.calib_load, "test_load": args.test_load,
        "calib_n": len(calib), "test_n": len(test),
        "rule": {"feature": best_f, "threshold": best_t, "calib_bal_acc": round(best_ba, 3)},
        "baseline_test": summarize(base_rows),
        "recalibrated_test": summarize(new_rows),
        "top5": [{"feature": f, "threshold": t, "calib_bal_acc": round(ba, 3)}
                 for ba, f, t in scored[:5]],
    }
    print(f"\n[旧阈值]    测试 accuracy={out['baseline_test']['accuracy']}  "
          f"召回={out['baseline_test']['fault_recall']}  normal误报={out['baseline_test']['normal_fp_rate']}")
    print(f"[重标定] {best_f}>{best_t:.4g}  测试 accuracy={out['recalibrated_test']['accuracy']}  "
          f"召回={out['recalibrated_test']['fault_recall']}  normal误报={out['recalibrated_test']['normal_fp_rate']}")
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
