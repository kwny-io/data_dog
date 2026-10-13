"""提示注入红队：把对抗性 case 打到**真模型**上，量化"数据里的指令会不会带跑它"。

为什么需要它
-----------
tests/test_adversarial_evals.py 用 MockDecisionProvider 跑 case_06——那只证明「评测器没变」，
**不证明真模型不被带跑**。红队要的是真模型上的失败率（哪怕只是抽样）：先说清楚自己有多脆，
才有资格谈"防住了"。

用法
----
    python scripts/redteam_injection.py --runs 5              # 真模型（需要 key，花 token）
    python scripts/redteam_injection.py --offline --runs 3    # 离线确定性 provider（自检管道）

判据与 case 定义一致：**不得**出现 forbids_disposition（典型是"无凭据 confirmed"）。
退出码：0 = 未被带跑；1 = 至少一次被带跑；2 = 环境问题（缺 key 等）。

诚实边界
--------
- 这是**抽样**，不是安全证明：runs 越大置信越强，但永远证不了"不会"。
- 只覆盖已知形态（case_06/07）；换模型、改系统提示、加新工具都该重跑。
- 即使被带跑，**副作用面也是有限的**：告警要过确定性策略门、工具只读且部件白名单，
  模型改不了这个事实（见 core/injection.py 的边界段）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from data_dog.core.console import ensure_utf8_console  # noqa: E402
from evals.cases import find_case  # noqa: E402
from evals.run_evals import MockDecisionProvider  # noqa: E402
from evals.runner import run_case  # noqa: E402

# 已知的对抗性 case（身份伪装与注入同属"输入不可信"这一类）
_TARGETS = ("case_06_prompt_injection", "case_07_identity_spoof")


def _build_provider(offline: bool):
    """离线 → (None, None)，每个 case 现造 Mock；否则用已配置的真实供应商。"""
    if offline:
        return None, None
    from data_dog.providers.client import Provider
    from data_dog.providers.config import load_config

    cfg = load_config(_ROOT)
    key = (cfg.get("api_key") or "").strip()
    if not key:
        print("未配置 LLM 凭据：先 data-dog init 设 key，或用 --offline 跑管道自检",
              file=sys.stderr)
        raise SystemExit(2)
    return (Provider.from_config(cfg["vendor_id"], key,
                                 base_url=cfg.get("base_url") or ""),
            cfg.get("model") or None)


def main() -> int:
    ap = argparse.ArgumentParser(description="提示注入红队（对抗性 case × 真模型）")
    ap.add_argument("--runs", type=int, default=3, help="每个 case 跑几次（默认 3）")
    ap.add_argument("--offline", action="store_true",
                    help="用离线确定性 provider 自检管道（不花 token，也不代表真模型行为）")
    ap.add_argument("--model", default="", help="覆盖模型名")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    ensure_utf8_console()

    provider, model = _build_provider(args.offline)
    runs = max(1, int(args.runs))
    rows: list[dict] = []
    breached = 0
    for case_id in _TARGETS:
        case = find_case(case_id)
        for i in range(runs):
            p = MockDecisionProvider(case_id) if args.offline else provider
            res = run_case(case, p, model=(args.model or model))
            ok = bool(res["checks"].get("disposition_not_forbidden", True))
            breached += 0 if ok else 1
            rows.append({"case": case_id, "run": i + 1, "ok": ok,
                         "disposition": res.get("disposition")})
            print(f"{'OK    ' if ok else 'BREACH'} {case_id} #{i + 1} "
                  f"disposition={res.get('disposition')}")

    out = {"mode": "offline" if args.offline else "real",
           "runs_per_case": runs, "breaches": breached, "total": len(rows), "rows": rows}
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    print(f"\n汇总：{len(rows) - breached}/{len(rows)} 未被带跑（breaches={breached}）")
    if args.offline:
        print("注意：离线档只验证**评测管道**；真模型结论必须不带 --offline 跑。")
    return 1 if breached else 0


if __name__ == "__main__":
    raise SystemExit(main())
