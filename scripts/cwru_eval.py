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
  这正是"跨数据集需要重新标定"的证据，不是 bug。重标定见 `scripts/cwru_recalibrate.py`。
- **转速用标称值**（`LOAD_RPM`）：本镜像仓库的 `XnnnRPM` 变量是**空的**，缺陷频率按标称转速算。
"""
from __future__ import annotations

import argparse
import json
import re
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
    LOAD_RPM,
    CWRUEvidenceSource,
    features,
    read_mat_variables,
)
from data_dog.providers.client import Provider  # noqa: E402
from data_dog.providers.config import load_config  # noqa: E402
from data_dog.schemas.events import CandidateEvent  # noqa: E402
from data_dog.tools.investigation import InvestigationTools  # noqa: E402

# 数据文件（data/ 已 gitignore）→ 标签。op_0 = 0 HP，op_1 = 1 HP（用于跨载荷 held-out）。
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
    "op_1/normal/98.mat": "normal",
    "op_1/ball_07/119.mat": "ball",
    "op_1/inner_07/106.mat": "inner",
    "op_1/outer_07/131.mat": "outer",
    "op_2/normal/99.mat": "normal",
    "op_2/ball_07/120.mat": "ball",
    "op_2/inner_07/107.mat": "inner",
    "op_2/outer_07/132.mat": "outer",
    "op_3/normal/100.mat": "normal",
    "op_3/ball_07/121.mat": "ball",
    "op_3/inner_07/108.mat": "inner",
    "op_3/outer_07/133.mat": "outer",
}
_FLAGGED = ("needs_human_review", "confirmed")

# ---------------------------------------------------------------- 采样率（重要）
# 官方文档只写了："Digital data was collected at 12,000 samples per second, and data was
# also collected at 48,000 samples per second for drive end bearing faults."
# ——**正常基线页没有写采样率**。用**样本数 ÷ CWRU 的录音时长（5 s / 10 s 两档）**反推：
#   · 12k 驱动端故障（105…133，169…237）：121,265 点 ≈ 10.1 s @12 kHz ✅
#   · 正常 98/99/100：483,903 / 485,063 / 485,643 点 ≈ 10.1 s @48 kHz ✅（@12 kHz 会是 40 s，
#     全库没有任何一条录音接近 40 s）
#   · 正常 97 与 48k 驱动端故障 109：都是 **243,938** 点 ≈ 5.08 s @48 kHz ✅（两者同为 0 HP）
# 结论：正常基线 = **48 kHz**。旧实现把所有文件都按 12 kHz 算包络 → normal 的 env_* 落在
# **错误的频带**（4× 偏），因此 §7 那批 normal 包络特征不可信。可用 --normal-fs 切回 12 kHz 做敏感性。
NORMAL_FS = 48000.0
FAULT_FS_12K = 12000.0
FAULT_FS_48K = 48000.0
_NORMAL_NUMS = {97, 98, 99, 100}
# 官方 "48k Drive End Bearing Fault Data" 页的文件号段（其余驱动端故障为 12k）
_DE_48K_NUMS = frozenset(
    list(range(109, 113)) + list(range(122, 126)) + list(range(135, 139))
    + list(range(174, 178)) + list(range(189, 193)) + list(range(201, 205))
    + list(range(213, 217)) + list(range(226, 230)) + list(range(238, 242)))


def file_number(rel: str) -> int:
    m = re.search(r"(\d+)\.mat", rel)
    return int(m.group(1)) if m else 0


def file_sample_rate(rel: str, normal_fs: float = NORMAL_FS) -> float:
    """按 CWRU 官方分组给每条文件定采样率（Hz）。"""
    n = file_number(rel)
    if n in _NORMAL_NUMS or "normal" in rel.replace("\\", "/"):
        return float(normal_fs)
    return FAULT_FS_48K if n in _DE_48K_NUMS else FAULT_FS_12K


def de_key(vars_: dict, rel: str) -> str | None:
    """选与文件编号匹配的 DE 变量。

    ⚠️ **99.mat 同时含 X098_DE_time 与 X099_DE_time**（CWRU 把 98/99 两条记录塞进同一文件）。
    旧实现取"第一个含 DE 的变量" → op_2/normal 实际读到的是 **98.mat 的同一条信号**，
    op_1 与 op_2 的 normal 完全重复（fold 不独立）。按编号取即可。
    """
    keys = [k for k in vars_ if "DE" in k]
    if not keys:
        return None
    num = str(file_number(rel))
    for k in keys:
        if num in k:
            return k
    return keys[0]


def load_index(rel: str) -> int:
    m = re.search(r"op_(\d+)", rel)
    return int(m.group(1)) if m else 0


def load_windows(data_dir: Path, per_file: int | None = None,
                 wlen: int | None = None, stride: int | None = None,
                 normal_fs: float = NORMAL_FS, *,
                 window_sec: float = 2.0, stride_sec: float | None = None
                 ) -> list[tuple[str, int, np.ndarray, float]]:
    """读每个文件的 DE 信号并切窗；返回 [(label, load, window, fs), ...]。

    ⚠️ **窗长必须按"秒"给，不能按"点数"给**。各文件采样率不同（正常 48 kHz、12k 故障 12 kHz），
    同一个"24000 点"在正常文件是 **0.5 s**、在故障文件是 **2 s**——积分时长不同，kurtosis /
    波峰因子这类统计量就不可比，包络谱的分辨率也随窗长变。实测：按点数切时 op_2 / op_3 各冒
    1~2 个 normal 误报；按同一物理时长切之后，四折 normal 误报**全为 0**（见 docs §9）。
    传 wlen / stride（点数）即回到旧口径，**仅用于复现历史结果**。
    """
    out: list[tuple[str, int, np.ndarray, float]] = []
    for rel, label in FILES.items():
        p = data_dir / rel
        if not p.exists():
            continue
        try:
            vars_ = read_mat_variables(p)
        except Exception as e:                         # noqa: BLE001
            print(f"[warn] {rel}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        key = de_key(vars_, rel)
        if key is None:
            print(f"[warn] {rel}: 无 DE 变量（有 {list(vars_)}）", file=sys.stderr)
            continue
        x = vars_[key]
        fs = file_sample_rate(rel, normal_fs)
        if wlen:                                       # 旧口径：固定点数
            w, st = int(wlen), int(stride) if stride else int(wlen)
        else:                                          # 推荐：固定物理时长
            w = max(8, int(round(window_sec * fs)))
            st = max(1, int(round((stride_sec or window_sec) * fs)))
        load = load_index(rel)
        n = 0
        for start in range(0, max(0, len(x) - w), st):
            if per_file is not None and n >= per_file:
                break
            out.append((label, load, x[start:start + w], fs))
            n += 1
    return out


def _cand(i: int, sample_count: int = 24000) -> CandidateEvent:
    # candidate_id 中性：不许把标签写进 id（评测污染）。sample_count 用**真实**窗长，别写死。
    return CandidateEvent(candidate_id=f"cand_cwru_{i:03d}", device_sn="device-cwru",
                          component_ref=BEARING_COMPONENT, signal="vibration_anomaly",
                          severity_hint="warning", window_start="w0", window_end="w0",
                          sample_count=int(sample_count), rule_matches=["CWRU_VIB"],
                          anomaly_score=0.5, detector_provenance="cwru")


def hit(label: str, disposition: str) -> bool:
    return (disposition in _FLAGGED) if label != "normal" else (disposition == "rejected")


def baseline_anomaly(label: str, x: np.ndarray) -> bool:
    f = features(x)
    anom = f.get("kurtosis", 0.0) > 4.0 or f.get("crest_factor", 0.0) > 5.0
    return hit(label, "needs_human_review" if anom else "rejected")


def summarize(rows: list[dict]) -> dict:
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
    ap.add_argument("--window-sec", type=float, default=2.0,
                    help="窗长（秒，推荐）——各文件按各自 fs 折点数，保证物理时长一致")
    ap.add_argument("--stride-sec", type=float, default=None,
                    help="步长（秒）；默认 = 窗长（不重叠窗口）")
    ap.add_argument("--window-len", type=int, default=None,
                    help="旧口径：固定**点数**窗长（跨采样率不可比，仅复现历史结果）")
    ap.add_argument("--stride", type=int, default=None, help="旧口径：固定**点数**步长")
    ap.add_argument("--loads", default="", help="只看某载荷，如 op_0 或 op_0,op_1")
    ap.add_argument("--normal-fs", type=float, default=NORMAL_FS,
                    help="正常基线采样率（默认 48000，见文件头证据；设 12000 做敏感性对照）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--baseline-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    ensure_utf8_console()

    wins = load_windows(Path(args.data_dir), args.windows_per_file, args.window_len,
                        args.stride, args.normal_fs,
                        window_sec=args.window_sec, stride_sec=args.stride_sec)
    if args.loads:
        want = {int(x) for x in re.findall(r"\d+", args.loads)}
        wins = [w for w in wins if w[1] in want]
    if not wins:
        print("没有可用窗口：先下载 data/cwru（见脚本内 FILES 清单）", file=sys.stderr)
        raise SystemExit(1)
    counts: dict = {}
    for lb, load, _x, _fs in wins:
        counts[f"op_{load}/{lb}"] = counts.get(f"op_{load}/{lb}", 0) + 1
    print(f"窗口 {len(wins)} 条（每文件 {args.windows_per_file}）| {counts}")

    base = [{"label": lb, "hit": baseline_anomaly(lb, x)} for lb, _load, x, _fs in wins]
    out = {"n": len(wins), "baseline": summarize(base)}
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
            label, load, x, fs = wins[i]
            tools = InvestigationTools(
                CWRUEvidenceSource(x, rpm=LOAD_RPM.get(load, 1797.0), fs=fs), ctx=ctx)
            rep = AgentLoop(provider, tools, model=model).investigate(_cand(i, len(x)))
            return {"label": label, "hit": hit(label, rep.disposition),
                    "disposition": rep.disposition}

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            rows = list(ex.map(one, range(len(wins))))
        out["llm"] = summarize(rows)
        print(f"[llm]      accuracy={out['llm']['accuracy']}  "
              f"故障召回={out['llm']['fault_recall']}  normal误报={out['llm']['normal_fp_rate']}")
        print(f"  分类命中: {out['llm']['by_fault']}")
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
