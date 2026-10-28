"""把 P1 算子（`Operator`）转成在线链用的 `DetectorConfig` —— **唯一**实现。

为什么单独成模块
----------------
这个转换原先在 `data_plane/p2_demo.py`、`e2e/replay.py`、`tests/test_data_plane.py`
各写了一份，三份都只搬 `fields` + `rules`，**丢掉了 `dsl_rules`**。后果是窗口/基线类
判据在离线 Fixture 回放里"能用"，一旦进在线链就静默不存在——检测能力被砍掉一半，
而且没有任何报错。一份实现才能保证"离线验证过的规则 = 线上跑的规则"。

层次：`schemas` 是叶子层，本模块只 import 它，不引入环。
"""
from __future__ import annotations

from typing import Any

from ..schemas.operator_config import DetectorConfig, DslRuleSpec, FieldSpec, ThresholdRule


def detector_config_from_operator(op: Any, *,
                                  operator_type: str = "parser+detector"
                                  ) -> DetectorConfig:
    """`Operator` → `DetectorConfig`（fields / rules / dsl_rules 三者全搬）。"""
    return DetectorConfig(
        operator_id=op.operator_id,
        operator_type=operator_type,
        component=op.component,
        input_schema=op.input_schema,
        output_schema=op.output_schema,
        fields=[FieldSpec(**f) for f in op.fields],
        rules=[ThresholdRule(r.rule_id, r.field, r.operator, r.value,
                             r.severity_hint, r.message, getattr(r, "provenance", ""),
                             getattr(r, "subsystem", ""))
               for r in op.rules],
        dsl_rules=[DslRuleSpec(
            rule_id=d["rule_id"], when=d["when"],
            severity_hint=d.get("severity_hint", "warning"),
            message=d.get("message", ""),
            field=d.get("field", ""),
            provenance=d.get("provenance", ""),
            subsystem=d.get("subsystem", ""),
        ) for d in op.dsl_rules],
        generator_provenance=op.generator_provenance,
    )
