"""单模块覆盖率下限门禁：总分会被"平均"掉，模块级塌陷必须能被发现。

为什么需要它
------------
`pytest --cov-fail-under` 只看**总分**。一个 500 行的核心模块掉到 20%，只要别处补上
同样的覆盖行数，总分照样过——于是"没人测的部分"可以长期存在，而且没有任何信号。
本脚本按**文件**检查下限，把这种塌陷变成 CI 红。

用法
----
    python -m pytest -q --cov=data_dog --cov-report=json:coverage.json
    python scripts/cov_gate.py --json coverage.json --min 60

豁免（`ALLOW`）：每条都必须写明理由，否则等于把门禁关掉。豁免的是"覆盖口径不适用"
的文件，不是"懒得测"的文件。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

# 豁免：路径片段 → 理由
ALLOW: dict[str, str] = {
    "_demo.py": "演示脚本（人读输出）：行为由 e2e 回放与对应子系统的单测共同覆盖，"
                "其 print 排版本身不值得断言",
    "_eval.py": "评估入口（跑真实 Agent / 仿真的编排壳）：判定口径由 synth_eval 与 "
                "dataset_eval 的单测覆盖，壳层本身只做汇总打印",
    "service/stream_worker.py": "长驻进程入口（main 的启停/信号路径）：由部署验收与 "
                                "tests/test_stream.py 的 runner 层覆盖，入口壳层不单独测",
    "mujoco_sim/simulate.py": "真仿真循环（mj_step）：需要真物理积分且耗时不可接受；"
                              "其纯逻辑（_pd_target/_get_model/参数校验）已单测",
    "ui/": "Streamlit 交互层",
    "mcp_servers/": "MCP 常驻进程适配层（协议握手由 tests/test_mcp_context.py 的桩覆盖）",
}


def _exempt(rel: str) -> str | None:
    for frag, why in ALLOW.items():
        if frag in rel:
            return why
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="按模块检查覆盖率下限")
    ap.add_argument("--json", default="coverage.json", help="--cov-report=json 产出的文件")
    ap.add_argument("--min", type=float, default=60.0, help="单模块下限（百分比）")
    ap.add_argument("--min-statements", type=int, default=30,
                    help="语句数少于该值的文件不参与（小文件抖动大、噪声多）")
    args = ap.parse_args()

    path = pathlib.Path(args.json)
    if not path.exists():
        print(f"[cov-gate] 找不到 {path}；先跑 "
              f"`pytest --cov=data_dog --cov-report=json:{path.name}`")
        return 2

    data = json.loads(path.read_text(encoding="utf-8"))
    total = data["totals"]["percent_covered"]
    offenders: list[tuple[float, int, int, str]] = []
    exempted: list[tuple[float, str]] = []

    for file_path, info in data["files"].items():
        rel = file_path.replace("\\", "/")
        summary = info["summary"]
        if summary["num_statements"] < args.min_statements:
            continue
        why = _exempt(rel)
        if why:
            exempted.append((summary["percent_covered"], rel))
            continue
        if summary["percent_covered"] < args.min:
            offenders.append((summary["percent_covered"], summary["num_statements"],
                              summary["missing_lines"], rel))

    print(f"[cov-gate] 总覆盖率 {total:.2f}%（总分门禁由 pytest --cov-fail-under 把关）；"
          f"单模块下限 {args.min}%（语句数 ≥{args.min_statements}）")
    if exempted:
        print(f"[cov-gate] 豁免 {len(exempted)} 个文件（理由见脚本 ALLOW）：")
        for pct, rel in sorted(exempted):
            print(f"    - {pct:5.1f}%  {rel}")
    for pct, stmts, miss, rel in sorted(offenders):
        print(f"    ✗ {pct:5.1f}%  {miss:>4} miss / {stmts:>4} stmts   {rel}")
    if offenders:
        print(f"\n[cov-gate] {len(offenders)} 个模块低于 {args.min}%：补测，"
              "或在 ALLOW 里写明「为什么这条覆盖口径不适用」。")
        return 1
    print("[cov-gate] 所有参与检查的模块都达标 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
