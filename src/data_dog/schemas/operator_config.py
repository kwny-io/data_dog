# -*- coding: utf-8 -*-
"""声明式算子配置（A1 后半段：从文档 → 低风险声明式算子，对齐架构 §8.3）。

架构原则：**配置优先、代码兜底**。能用声明式映射/受限DSL表达时，不生成通用代码。
算子 = 一段可校验、可版本化、可注册的 JSON 声明（不是任意可执行代码），从而
满足"不可信输入默认隔离"——生成器产出的是配置，不是会直接执行的 Python/WASM。

本模型对应架构 §10.2 的 Operator Manifest 的**精华子集**，聚焦解析/标准化/简单检测，
不含沙箱/生命周期字段（那属于后续阶段）。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class FieldSpec:
    """声明式字段映射：日志/原始数据的字段如何标准命名、单位、类型。"""
    name: str                       # 标准字段名，如 temperature_c
    source: str                     # 原始字段名/来源
    type: str                       # uint8/float32/int16 等（来自 IDL 或文档）
    unit: Optional[str] = None      # celsius / ampere / rpm ...
    optional: bool = False


@dataclass
class ThresholdRule:
    """声明式检测规则：字段 + 比较 + 阈值（来自文档的阈值/错误码）。"""
    rule_id: str                    # 唯一 id，如 MOTOR_TEMP_TREND
    field: str                      # 作用字段
    operator: str                   # gt / lt / ge / le / eq
    value: float
    severity_hint: str              # warning / critical
    message: str                    # 触发时的说明


@dataclass
class DetectorConfig:
    """一个部件的声明式检测算子配置。"""
    operator_id: str                # 如 vendor-a.motor-parser / motor-health-detector
    operator_type: str              # parser / normalizer / detector
    component: str                  # 如 joint_motor.front_left_hip
    input_schema: str
    output_schema: str
    fields: List[FieldSpec] = field(default_factory=list)
    rules: List[ThresholdRule] = field(default_factory=list)
    generator_provenance: str = ""  # 生成依据的知识来源

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
