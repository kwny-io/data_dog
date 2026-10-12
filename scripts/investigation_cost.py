"""单次调查的**成本 / 延迟分布**（真 LLM）：P50/P95/P99 tokens / 成本 / 墙钟 + 超预算率。

为什么要有它
-----------
`docs/slo.md` §5 写了"单次调查成本目标 ≤ ¥0.05（上限 ¥0.20）"，但**没有实测分布**。
工业选型第一问是"一次多少钱、多久、尾部多长"——所以给的是**分布**，不是均值。

口径与边界
---------
- token / 耗时来自 provider 的真实 usage（**硬数字**）。
- 成本按 `--price-in/--price-out`（USD / 1M tokens）折算；默认取
  `DD_LLM_PRICE_IN/OUT`，都没有则用 DeepSeek 公开价当**参考价**（明确标注）。
  **网关实际价请自己覆盖**（`--price-in/--price-out`）。
- 墙钟含 Agent 多步 ReAct，受网络/并发影响；**同机同窗口的相对对照才可比**。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

from visual_eval import build_samples  # noqa: E402

from data_dog.core.console import ensure_utf8_console  # noqa: E402
from data_dog.providers.client import Provider  # noqa: E402
from data_dog.providers.config import load_config  # noqa: E402
from data_dog.synth.synth_eval import eval_one  # noqa: E402

_DS_IN, _DS_OUT = 0.27, 1.10     # DeepSeek 公开价（USD/1M tokens），仅作参考价兜底


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round((len(s) - 1) * q))))
    return s[i]


def main() -> int:
    ap = argparse.ArgumentParser(description="单次调查成本/延迟分布（真 LLM）")
    ap.add_argument("--n-per-class", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--price-in", type=float, default=None, help="USD/1M 输入 token")
    ap.add_argument("--price-out", type=float, default=None, help="USD/1M 输出 token")
    ap.add_argument("--usd-per-cny", type=float, default=7.1)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    ensure_utf8_console()      # Windows 控制台默认 GBK，装不下 ¥ 等字符

    cfg = load_config(_ROOT)
    if not cfg["api_key"]:
        print("需要先配 key（.env）", file=sys.stderr)
        return 1
    from data_dog import settings
    pin = args.price_in if args.price_in is not None else (settings.llm_price_in() or _DS_IN)
    pout = args.price_out if args.price_out is not None else (settings.llm_price_out() or _DS_OUT)
    priced = bool(args.price_in is not None or args.price_out is not None
                  or settings.llm_price_in() or settings.llm_price_out())

    provider = Provider.from_config(cfg["vendor_id"], cfg["api_key"],
                                    base_url=cfg.get("base_url") or "")
    model = cfg["model"] or None
    samples, labels = build_samples(args.n_per_class)
    print(f"样本 {len(samples)} 条（每类 {args.n_per_class}）| model={model} | "
          f"价 {pin}/{pout} USD/1M（{'自声明' if priced else '参考价'}）")

    def one(i: int) -> dict:
        t0 = time.perf_counter()
        r = eval_one(samples[i], labels[i], provider, model=model)
        wall = (time.perf_counter() - t0) * 1000.0
        u = r.get("usage") or {}
        pt, ct = int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
        cost = pt / 1e6 * pin + ct / 1e6 * pout
        return {"wall_ms": wall, "llm_ms": float(u.get("duration_ms", 0.0)),
                "calls": int(u.get("call_count", 0)), "pt": pt, "ct": ct,
                "cost_usd": cost, "hit": r["hit"], "fault": r["fault"],
                "disposition": r["agent_disposition"]}

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        rows = list(ex.map(one, range(len(samples))))

    walls = [x["wall_ms"] for x in rows]
    llms = [x["llm_ms"] for x in rows]
    costs = [x["cost_usd"] for x in rows]
    toks = [x["pt"] + x["ct"] for x in rows]
    calls = [x["calls"] for x in rows]
    cny = args.usd_per_cny
    tgt, cap = 0.05 / cny, 0.20 / cny        # USD
    out = {
        "n": len(rows), "model": model, "base_url": cfg["base_url"],
        "price_in": pin, "price_out": pout, "price_self_declared": priced,
        "calls": {"p50": pct(calls, .5), "p95": pct(calls, .95)},
        "tokens": {"p50": pct(toks, .5), "p95": pct(toks, .95), "p99": pct(toks, .99)},
        "cost_usd": {"p50": pct(costs, .5), "p95": pct(costs, .95), "p99": pct(costs, .99),
                     "mean": statistics.fmean(costs) if costs else 0.0},
        "cost_cny": {"p50": pct(costs, .5) * cny, "p95": pct(costs, .95) * cny,
                     "p99": pct(costs, .99) * cny, "mean": (statistics.fmean(costs) * cny) if costs else 0.0},
        "llm_ms": {"p50": pct(llms, .5), "p95": pct(llms, .95), "p99": pct(llms, .99)},
        "wall_ms": {"p50": pct(walls, .5), "p95": pct(walls, .95), "p99": pct(walls, .99)},
        "over_target_rate": round(sum(1 for c in costs if c > tgt) / len(rows), 3) if rows else 0.0,
        "over_cap_rate": round(sum(1 for c in costs if c > cap) / len(rows), 3) if rows else 0.0,
        "dispositions": {d: sum(1 for x in rows if x["disposition"] == d)
                         for d in sorted({x["disposition"] for x in rows})},
    }
    print(f"调用次数/调查 : P50={out['calls']['p50']}  P95={out['calls']['p95']}")
    print(f"总 tokens     : P50={out['tokens']['p50']:.0f}  P95={out['tokens']['p95']:.0f}  P99={out['tokens']['p99']:.0f}")
    print(f"成本(¥)       : P50={out['cost_cny']['p50']:.4f}  P95={out['cost_cny']['p95']:.4f}  "
          f"P99={out['cost_cny']['p99']:.4f}  均值={out['cost_cny']['mean']:.4f}")
    print(f"LLM 网络耗时  : P50={out['llm_ms']['p50']:.0f}ms  P95={out['llm_ms']['p95']:.0f}ms  P99={out['llm_ms']['p99']:.0f}ms")
    print(f"调查墙钟      : P50={out['wall_ms']['p50']:.0f}ms  P95={out['wall_ms']['p95']:.0f}ms  P99={out['wall_ms']['p99']:.0f}ms")
    print(f"超目标(>¥0.05)率 = {out['over_target_rate']}   超上限(>¥0.20)率 = {out['over_cap_rate']}")
    print(f"裁决分布      : {out['dispositions']}")
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
