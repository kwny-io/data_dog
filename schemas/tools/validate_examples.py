# -*- coding: utf-8 -*-
"""校验 schemas/ 下的正式事件 Schema：正例必须通过，反例必须被拒。

用法：
    python schemas/tools/validate_examples.py

为什么单独写：P0-1 的验收标准不是"写出 JSON"，而是"Schema 真能区分合规/不合规实例"。
每个 Schema 配一对 example：<name>.valid.json（必须通过）+ <name>.invalid.json（必须拒绝）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parent.parent   # schemas/
EX_DIR = ROOT / "examples"

SCHEMAS = {
    "raw-log-envelope-v1": "raw-log-envelope-v1.json",
    "normalized-telemetry-event-v1": "normalized-telemetry-event-v1.json",
    "candidate-event-v1": "candidate-event-v1.json",
    "investigation-report-v1": "investigation-report-v1.json",
    "saas-alert-event-v1": "saas-alert-event-v1.json",
}


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> int:
    failures: list[str] = []
    checked = 0
    for name, schema_file in SCHEMAS.items():
        schema = _load(ROOT / schema_file)
        # 1) Schema 自身合法
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as e:
            failures.append(f"[{name}] Schema 自身非法: {e.message}")
            continue

        valid_p = EX_DIR / f"{name}.valid.json"
        invalid_p = EX_DIR / f"{name}.invalid.json"
        if not valid_p.exists() or not invalid_p.exists():
            failures.append(f"[{name}] 缺示例: {valid_p.name} / {invalid_p.name}")
            continue

        # 2) 正例必须通过
        try:
            jsonschema.validate(_load(valid_p), schema)
            ok_valid = True
        except jsonschema.ValidationError as e:
            ok_valid = False
            failures.append(f"[{name}] 正例被误拒: {e.message} @ {'/'.join(map(str, e.path))}")
        # 3) 反例必须被拒
        try:
            jsonschema.validate(_load(invalid_p), schema)
            ok_invalid = False
            failures.append(f"[{name}] 反例未被拒（Schema 太宽松）")
        except jsonschema.ValidationError:
            ok_invalid = True

        if ok_valid and ok_invalid:
            print(f"  ✅ {name}: 正例通过 / 反例被拒")
        checked += 1

    print()
    if failures:
        for f in failures:
            print(f"  ❌ {f}")
        print(f"\n❌ {len(failures)} 项失败（检查了 {checked} 个 Schema）")
        return 1
    print(f"✅ 全部通过：{checked} 个 Schema，正例通过 + 反例被拒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
