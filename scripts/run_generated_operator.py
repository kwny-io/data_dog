"""演示：把生成的声明式检测算子真正跑起来。

读 operators/generated/*.yaml → 对一个部件的遥测（这里用 mock 的一条）
应用其 ThresholdRule(minimal engine) → 输出命中规则 / Candidate 摘要。

目的：证明 A1 生成的"声明式算子"是可运行、可验证的（不是死配置）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OPERATORS = ROOT / "operators" / "generated"
MOCK_TELEMETRY = {
    "joint_motor.front_left_hip": {"temperature": 92, "current": 6.0, "dq": 0.1},
    "battery.main": {"soc": 55, "current": 18.0},
}


def run_detector(cfg: dict, tele: dict) -> list[str]:
    """把声明的检测规则应用到遥测，返回命中的 rule_id。"""
    hits = []
    for rule in cfg.get("rules", []):
        field = rule["field"]
        if field not in tele:
            continue
        val = tele[field]
        op = rule["operator"]
        thr = rule["value"]
        ok = {
            "gt": val > thr, "lt": val < thr,
            "ge": val >= thr, "le": val <= thr, "eq": val == thr,
        }.get(op, False)
        if ok:
            hits.append(f"[{rule['severity_hint']}] {rule['rule_id']} "
                        f"({field}={val} {op} {thr}) — {rule['message']}")
    return hits


def main() -> int:
    tested = 0
    for path in sorted(OPERATORS.glob("*.yaml")):
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        comp = cfg["component"]
        tele = MOCK_TELEMETRY.get(comp)
        if tele is None:
            continue
        hits = run_detector(cfg, tele)
        tag = ", ".join(hits) if hits else "无命中（正常）"
        print(f"[{cfg['operator_id']}] 遥测 {comp} → {tag}")
        tested += 1
    print(f"\n✅ 运行 {tested} 个检测算子（对 mock 遥测）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
