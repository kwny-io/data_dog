# -*- coding: utf-8 -*-
"""P1 端到端：厂商文档 → 知识包 → 检测算子（声明式+DSL）→ Fixture 回放验证。

跑法：
    python -m data_dog.operator_factory.p1_demo          # 人读输出
    python -m data_dog.operator_factory.p1_demo --json   # 机器可读

诚实性：阈值**只来自文档真有的规格**；文档里没有的电机 75/90°C 阈值**不会**被编造成
厂商事实——它们以 `unconfirmed` 出现在知识包里，DSL 规则里若要用必须显式声明为
"项目假设(assumption)"并在 provenance 里标注，绝不冒充厂商文档。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..paths import vendor_docs_dir
from .dsl import DSLError
from .knowledge import build_knowledge_package, flag_unconfirmed
from .pipeline import DeclarativeRule, Fixture, Operator, replay_all

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def cleaned_dir() -> Path:
    """厂商清洗后文档目录（运行时解析，尊重 DD_VENDOR_DOCS 覆盖）。"""
    return vendor_docs_dir() / "unitree" / "cleaned"


def build_motor_operator(pkg) -> Operator:
    """从知识包装配 front_left_hip 电机检测算子。

    - fields：来自 IDL 字段规格（真实字段）
    - rules（声明式）：**只用有权威出处的数值**——电机温度保护阈值 90°C 来自官方开源协议头
      （unitree_actuator_sdk，BSD-3），标记为厂商事实。
    - dsl_rules：多字段窗口 + 位运算；其中 75°C warning 阈**无权威出处**，显式标为项目假设。
    """
    fields = [
        {"name": f.name, "source": f.name, "type": f.type, "unit": f.unit,
         "optional": False}
        for f in pkg.fields if f.name in
        ("mode", "temperature", "current", "dq", "tau_est", "lost", "q")
    ]
    seen = set()
    uniq_fields = []
    for f in fields:
        if f["name"] in seen:
            continue
        seen.add(f["name"])
        uniq_fields.append(f)

    # 从知识包取**有出处**的电机温度保护阈值（官方开源协议头）
    protect = next((s for s in pkg.numeric_specs
                    if s.key == "motor_temp_protect_c"), None)
    protect_c = protect.value if protect else None
    provenance = (
        "IDL fields from unitree_sdk2 MotorState IDL; "
        f"temperature protection threshold {protect_c}°C VERIFIED from "
        "unitree_actuator_sdk/motor_msg_GO-M8010-6.h (BSD-3-Clause, official); "
        "75°C warning threshold is a PROJECT ASSUMPTION (no source found)"
    )

    rules = [
        # 健全性检查：命中「解析不出合理值」的情形才算异常。
        # 注意必须是 lt（负值）而不是 ge（非负）——写成 ge 0 会对**每一条**正常读数
        # 都命中，于是所有正常流量都会变成"候选异常"，淹没真正的异常。
        DeclarativeRule(
            rule_id="FIELD_SANITY_TEMP_INVALID",
            field="temperature", operator="lt", value=0.0,
            severity_hint="info",
            message="温度字段为负值（载荷或解析异常，非厂商阈值）",
        ),
    ]
    if protect_c is not None:
        rules.append(DeclarativeRule(
            rule_id="MOTOR_TEMP_PROTECT",
            field="temperature", operator="ge", value=float(protect_c),
            severity_hint="critical",
            message=f"电机温度达 {protect_c}°C 触发温度保护（厂商开源协议头，verified）",
        ))

    dsl_rules = [
        {
            "rule_id": "MOTOR_TEMP_RISE_UNDER_LOAD",
            "when": ("window.mean(temperature) >= 75 and "
                     "window.mean(current) <= 1.2 * window.baseline(current)"),
            "severity_hint": "warning",
            "message": "负载未显著上升但温度持续偏高（项目假设阈值 75°C，非厂商明写）",
        },
        {
            "rule_id": "MOTOR_MODE_FLAG_SET",
            "when": "(mode & 0x04) != 0",
            "severity_hint": "critical",
            "message": "电机 mode 第 2 位置位（位语义为项目假设，IDL 未给枚举含义）",
        },
    ]

    return Operator(
        operator_id="local.joint_motor-motorstate-detector",
        component="joint_motor.front_left_hip",
        input_schema="raw-log-envelope-v1",
        output_schema="normalized-telemetry-v1",
        fields=uniq_fields,
        rules=rules,
        dsl_rules=dsl_rules,
        generator_provenance=provenance,
        why_not_lower_layer=(
            "多字段 + 窗口均值/基线比较无法用单字段阈值表达；仍未到 WASM（无需自定义协议）"
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    pkg = build_knowledge_package(cleaned_dir())
    # 诚实登记：90°C 保护阈已有官方出处（verified）；仅剩 75°C warning 无出处
    if not any(s.key == "motor_temp_protect_c" for s in pkg.numeric_specs):
        flag_unconfirmed(pkg, ["motor_temp_protect_c"])
    flag_unconfirmed(pkg, ["motor_temp_warning_c"])

    op = build_motor_operator(pkg)

    # 编译 DSL（语法/白名单/类型错误在此暴露）
    try:
        compiled = op.compile_dsl()
    except DSLError as e:
        print(f"❌ DSL 编译失败: {e}")
        return 2

    fixtures = [Fixture.load(p) for p in sorted(FIXTURES.glob("*.json"))]
    summary = replay_all(op, fixtures)

    if args.json:
        print(json.dumps({
            "knowledge": {
                "revisions": len(pkg.revisions),
                "fields": len(pkg.fields),
                "numeric_specs": len(pkg.numeric_specs),
                "error_codes": len(pkg.error_codes),
                "verified_temp_protect": next(
                    (s.value for s in pkg.numeric_specs
                     if s.key == "motor_temp_protect_c"), None),
                "unconfirmed": [u.key for u in pkg.unconfirmed],
            },
            "operator": {
                "operator_id": op.operator_id,
                "fields": len(op.fields),
                "declarative_rules": len(op.rules),
                "dsl_rules": len(op.dsl_rules),
                "dsl_compiled": len(compiled),
            },
            "replay": summary,
        }, ensure_ascii=False, indent=2))
        return 0

    print("=" * 64)
    print("P1 离线算子工厂 —— 端到端回放")
    print("=" * 64)
    print(f"知识包：{len(pkg.revisions)} 份 revision，{len(pkg.fields)} 字段规格，"
          f"{len(pkg.numeric_specs)} 条数值规格，{len(pkg.error_codes)} 个错误码")
    prot = next((s for s in pkg.numeric_specs if s.key == "motor_temp_protect_c"), None)
    if prot:
        print(f"  ✅ 温度保护阈值 {prot.value}°C —— verified 于 {prot.source} ({prot.locator})")
    print(f"  待确认（无出处，不冒充厂商事实）：{[u.key for u in pkg.unconfirmed]}")
    print(f"算子  ：{op.operator_id}")
    print(f"  {len(op.fields)} 字段 / {len(op.rules)} 声明式规则 / "
          f"{len(op.dsl_rules)} DSL 规则（编译通过 {len(compiled)}）")
    print(f"  why_not_lower_layer: {op.why_not_lower_layer[:60]}...")
    print("-" * 64)
    for r in summary["results"]:
        mark = "✅" if r["passed"] else "❌"
        print(f"  {mark} {r['fixture_id']}: {r['detail']}")
    print("-" * 64)
    print(f"回放结果：{summary['passed']}/{summary['n']}  准确率 {summary['accuracy']}")
    return 0 if summary["passed"] == summary["n"] else 1


if __name__ == "__main__":
    sys.exit(main())
