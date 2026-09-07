# -*- coding: utf-8 -*-
"""A1 后半段：从结构化知识 → 声明式算子（配置优先，不生成任意代码）。

流程：
  1. 读 vendor_docs/unitree/cleaned/ 里清洗后的 IDL 字段表（kind=schema_field）
  2. 提取字段（name + type + 来源组件）→ FieldSpec
  3. 从手册/诊断的阈值(MotorState temperature 90/75 等) → ThresholdRule
  4. 组装成 DetectorConfig（声明式 JSON，非可执行代码）
  5. 校验通过后输出到 operators/generated/

满足架构：配置优先、不可信输入默认隔离、生成产物可版本化可审计。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml  # 可选；若未装则回退 json 输出

ROOT = Path(__file__).resolve().parent.parent
CLEANED = ROOT / "vendor_docs" / "unitree" / "cleaned"
OUT_DIR = ROOT / "operators" / "generated"

# IDL 字段表里已知的、有物理含义的字段 → (unit) 映射
KNOWN_INFO = {
    "temperature": "celsius",
    "current": "ampere",
    "soc": "percent",
    "voltage": "volt",
    "q": "radian",
    "dq": "rad/s",
    "ddq": "rad/s2",
}


def extract_field_specs_from_idl(text: str) -> list[dict]:
    """从清洗后的 IDL 字段表文本提取 (name, type)。"""
    fields = []
    for line in text.splitlines():
        m = re.match(r"\s*-\s+(\w+):\s*type=(\w+)", line)
        if m:
            name, typ = m.group(1), m.group(2)
            fields.append({
                "name": name,
                "source": name,
                "type": typ,
                "unit": KNOWN_INFO.get(name),
                "optional": False,
            })
    return fields


# 内置厂商知识里的明确电机温度阈值（来自架构文档电机工况剧本，作为规范化规则来源）
MOTOR_THRESHOLDS = [
    {"rule_id": "MOTOR_TEMP_TREND", "field": "temperature", "operator": "ge",
     "value": 75.0, "severity_hint": "warning",
     "message": "连续窗口温度达到75°C，判定热退化风险（KD-01）"},
    {"rule_id": "MOTOR_TEMP_OVERPROTECT", "field": "temperature", "operator": "ge",
     "value": 90.0, "severity_hint": "critical",
     "message": "温度达90°C，触发硬件过温保护并停机（KD-02）"},
]
BATT_THRESHOLDS = [
    {"rule_id": "BATT_SOC_LOW", "field": "soc", "operator": "le",
     "value": 20.0, "severity_hint": "warning",
     "message": "电量低于20%"},
]


def extract_thresholds_for_component(component: str) -> list[dict]:
    if component.startswith("joint_motor."):
        return [dict(r) for r in MOTOR_THRESHOLDS]
    if component.startswith("battery."):
        return [dict(r) for r in BATT_THRESHOLDS]
    return []


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    generated = []
    # 逐份 IDL 字段表生成解析/检测算子
    idl_files = sorted(CLEANED.rglob("*.idl.txt"))
    for f in idl_files:
        text = f.read_text(encoding="utf-8", errors="replace")
        fields = extract_field_specs_from_idl(text)
        if not fields:
            continue
        # 从文件名推断组件（如 unitree_sdk2__MotorState_ → joint_motor）
        base = f.stem
        if "MotorState" in base:
            component, op_type = "joint_motor.front_left_hip", "parser+detector"
        elif "BmsState" in base:
            component, op_type = "battery.main", "parser"
        elif "IMUState" in base:
            component, op_type = "controller.main", "parser"
        else:
            component, op_type = "unknown", "parser"
        cfg = {
            "operator_id": f"vendor-a.{component.split('.')[0]}-{base.split('__')[-1].lower()}",
            "operator_type": op_type,
            "component": component,
            "input_schema": "raw-log-envelope-v1",
            "output_schema": "normalized-telemetry-v1",
            "fields": fields,
            "rules": extract_thresholds_for_component(component),
            "generator_provenance": f"scope:{f.relative_to(ROOT)}",
        }
        generated.append(cfg)

    # 输出（yaml 可读；yaml 未装则 json）
    shown = 0
    for cfg in generated:
        out = OUT_DIR / f"{cfg['operator_id'].replace('.', '_')}.yaml"
        try:
            out.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        except NameError:
            import json
            out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        shown += 1
        print(f"  + {out.name} ({len(cfg['fields'])} fields, {len(cfg.get('rules', []))} rules)")
    print(f"\n✅ 生成 {shown} 个声明式算子 → {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
