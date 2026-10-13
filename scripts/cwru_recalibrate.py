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

from cwru_eval import NORMAL_FS, hit, load_windows, summarize  # noqa: E402

from data_dog.core.console import ensure_utf8_console  # noqa: E402
from data_dog.evidence.cwru import (  # noqa: E402
    LOAD_RPM,
    envelope_features,
    features,
)


def featurize(label: str, load: int, x: np.ndarray, fs: float = 12000.0) -> dict:
    """fs 必须与**该文件的真实采样率**一致，否则包络谱的缺陷频率带会整体偏移。

    正常基线是 48 kHz（证据见 cwru_eval 文件头），旧版把它当 12 kHz →
    normal 的 env_* 落在错误频带上，跨工况比较因此失真。
    """
    fr = LOAD_RPM.get(load, 1797.0) / 60.0
    f = {**features(x), **envelope_features(x, fs=fs, fr=fr)}
    # env_max（三类缺陷频率的最大值）由 evidence/cwru.envelope_features 提供——
    # 只看 BPFI 会漏外圈故障，见 docs/cross_dataset_cwru.md §9.3。
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


# 缺陷频带候选：三个单带 + "任一被抬升"。fit_margin_rule 在其中**按数据选**，不写死 BPFI。
ENV_BANDS = ("env_bpfo", "env_bpfi", "env_bsf", "env_max")


def fit_margin_rule(calib: list[dict], margin: float = 0.1) -> tuple[str, float, float]:
    """**零误报约束**下的双条件规则：缺陷频带证据 OR 时域冲击性。

    阈值 = 标定集**正常样本上界 × (1+margin)**——工程上就是"报警线压在健康基线最坏值之上"，
    不假设分布，也不做分位数扫描。分位数扫描（旧口径）会把阈值压进正常分布**内部**：
    它最大化的是平衡准确率，正常样本还剩一半余量就够。后果是当**被留出载荷的正常上界高于
    标定集的正常上界**时（op_3 的正常 env_bpfi 就是四个载荷里最高的），阈值落在测试载荷的
    正常范围里 → 误报。零误报约束把这个机制掐掉。

    主判据在 ENV_BANDS 里选**标定集上零误报、召回最高**的那个；第二条件固定为 kurtosis
    （早期微弱缺陷常先抬峭度，而包络带不一定命中）。
    """
    normals = [r for r in calib if r["__label__"] == "normal"]
    faults = [r for r in calib if r["__label__"] != "normal"]
    best: tuple[float, str, float] | None = None
    for band in ENV_BANDS:
        if band not in normals[0]:
            continue
        thr = max(r[band] for r in normals) * (1.0 + margin)
        rec = (sum(1 for r in faults if r[band] > thr) / len(faults)) if faults else 0.0
        if best is None or rec > best[0]:
            best = (rec, band, thr)
    assert best is not None, "标定集里没有 normal 样本"
    _, band, thr1 = best
    thr2 = max(r["kurtosis"] for r in normals) * (1.0 + margin)
    return band, thr1, thr2


def eval_margin_rule(rows: list[dict], rule: tuple[str, float, float]) -> list[dict]:
    band, thr1, thr2 = rule
    return [{"label": r["__label__"],
             "hit": hit(r["__label__"],
                        "needs_human_review"
                        if (r[band] > thr1 or r["kurtosis"] > thr2) else "rejected")}
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
    ap.add_argument("--rule", choices=("margin-or", "quantile"), default="margin-or",
                    help="margin-or（默认，零误报约束：阈值=标定集正常上界×(1+margin)，"
                         "缺陷频带 OR 峭度）| quantile（旧口径，见 docs §8.3）")
    ap.add_argument("--margin", type=float, default=0.10,
                    help="margin-or 的报警余量（相对标定集正常上界）")
    ap.add_argument("--windows-per-file", type=int, default=4)
    ap.add_argument("--window-sec", type=float, default=2.0,
                    help="窗长（秒，推荐）——各文件按各自 fs 折点数，保证物理时长一致")
    ap.add_argument("--stride-sec", type=float, default=None,
                    help="步长（秒）；默认 = 窗长（不重叠）")
    ap.add_argument("--window-len", type=int, default=None,
                    help="旧口径：固定**点数**窗长（跨采样率不可比，仅复现历史结果）")
    ap.add_argument("--stride", type=int, default=None, help="旧口径：固定**点数**步长")
    ap.add_argument("--normal-fs", type=float, default=NORMAL_FS,
                    help="正常基线采样率（默认 48000，见 cwru_eval 文件头证据；设 12000 做敏感性对照）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    ensure_utf8_console()

    wins = load_windows(Path(args.data_dir), args.windows_per_file,
                        args.window_len, args.stride, args.normal_fs,
                        window_sec=args.window_sec, stride_sec=args.stride_sec)
    rows_all = [featurize(lb, ld, x, fs) for lb, ld, x, fs in wins]
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
        scored: list[tuple[float, str, float]] = []
        if args.rule == "margin-or":
            rule = fit_margin_rule(calib, args.margin)
            band, thr1, thr2 = rule
            new_rows = eval_margin_rule(test, rule)
            rule_json = {"type": "margin-or", "feature": band, "threshold": round(thr1, 5),
                         "kurtosis_threshold": round(thr2, 3), "margin": args.margin}
            rule_desc = f"{band}>{thr1:.4g} 或 kurtosis>{thr2:.3f}"
        else:
            best_f, best_t, scored = fit_rule(calib, INVARIANT if args.invariant else None)
            new_rows = eval_rule(test, best_f, best_t)
            rule_json = {"type": "quantile", "feature": best_f, "threshold": round(best_t, 5)}
            rule_desc = f"{best_f}>{best_t:.4g}"
        old_rows = baseline_rows(test)
        b = summarize(old_rows)
        n = summarize(new_rows)
        agg_base += old_rows
        agg_new += new_rows
        fold: dict = {"test_load": test_load, "calib_n": len(calib), "test_n": len(test),
                      "rule": rule_json, "baseline": b, "recalibrated": n}
        if scored:
            fold["top3"] = [{"feature": f, "threshold": round(t, 5), "calib_bal_acc": round(ba, 3)}
                            for ba, f, t in scored[:3]]
        folds.append(fold)
        n_norm = sum(1 for r in test if r["__label__"] == "normal")
        print(f"fold test=op_{test_load} (calib {len(calib)} / test {len(test)} 窗口, "
              f"normal {n_norm})  规则 {rule_desc}  "
              f"旧 acc={b['accuracy']}/召回={b['fault_recall']}/FP={b['normal_fp_rate']}  "
              f"新 acc={n['accuracy']}/召回={n['fault_recall']}/FP={n['normal_fp_rate']}")

    out = {"loads": loads, "windows_per_file": args.windows_per_file,
           "window_sec": args.window_sec, "normal_fs": args.normal_fs,
           "rule": args.rule, "margin": args.margin, "folds": folds}
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
