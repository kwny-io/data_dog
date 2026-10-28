"""声明式算子配置（A1 后半段：从文档 → 低风险声明式算子，对齐架构 §8.3）。

架构原则：**配置优先、代码兜底**。能用声明式映射/受限DSL表达时，不生成通用代码。
算子 = 一段可校验、可版本化、可注册的 JSON 声明（不是任意可执行代码），从而
满足"不可信输入默认隔离"——生成器产出的是配置，不是会直接执行的 Python/WASM。

本模型对应架构 §10.2 的 Operator Manifest 的**精华子集**，聚焦解析/标准化/简单检测，
不含沙箱/生命周期字段（那属于后续阶段）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class FieldSpec:
    """声明式字段映射：日志/原始数据的字段如何标准命名、单位、类型。"""
    name: str                       # 标准字段名，如 temperature_c
    source: str                     # 原始字段名/来源
    type: str                       # uint8/float32/int16 等（来自 IDL 或文档）
    unit: str | None = None      # celsius / ampere / rpm ...
    optional: bool = False
    # 该字段的出处标记：verified（IDL/厂商文档明写）/ inferred（按命名约定推断，
    # 或由采集侧派生）/ assumption（项目假设）。默认空 = 未标注（旧调用点不变）。
    provenance: str = ""
    # 字段所属的厂商结构体（MotorState / BmsState / IMUState / ...）。空 = 不是
    # 厂商 IDL 字段（如项目自定义协议里的每关节电流）。同名字段跨结构体时，没有
    # 这一维就分不清"电机温度"和"IMU 温度"。
    structure: str = ""


@dataclass
class ThresholdRule:
    """声明式检测规则：字段 + 比较 + 阈值（来自文档的阈值/错误码）。"""
    rule_id: str                    # 唯一 id，如 MOTOR_TEMP_TREND
    field: str                      # 作用字段
    operator: str                   # gt / lt / ge / le / eq
    value: float
    severity_hint: str              # warning / critical
    message: str                    # 触发时的说明
    # 阈值出处的机器可读标记：verified（厂商文档明写）/ assumption（项目假设）。
    # 新增字段带默认值，故旧构造点（位置参数只给前 6 个）行为不变。
    provenance: str = ""
    # 故障所属子系统（同 DslRuleSpec）。空 = 未分类。
    subsystem: str = ""


@dataclass
class DslRuleSpec:
    """DSL 规则声明：多在窗口/基线上求值，单条阈值表达不了（对齐 §8.3）。

    只放**声明**（字符串表达式），求值仍由 `operator_factory.dsl` 的受限解释器负责。
    这里不引入任何可执行代码——与 ThresholdRule 同为配置，不违背"配置优先、代码兜底"。
    """
    rule_id: str
    when: str                       # DSL 表达式，如 window.slope(temperature) > 0.005
    severity_hint: str = "warning"
    message: str = ""
    field: str = ""                 # 证据归因字段；空则取表达式里唯一的字段引用
    provenance: str = ""            # verified / assumption
    # 故障所属子系统（A 关节电机 / B 热 / C 电源 / D 姿态 / E 通信 / F 整机步态）。
    # 运行期命中会带上它，下游才能按子系统聚合展示；空 = 未分类。
    subsystem: str = ""


@dataclass
class DetectorConfig:
    """一个部件的声明式检测算子配置。"""
    operator_id: str                # 如 vendor-a.motor-parser / motor-health-detector
    operator_type: str              # parser / normalizer / detector
    component: str                  # 如 joint_motor.front_left_hip
    input_schema: str
    output_schema: str
    fields: list[FieldSpec] = field(default_factory=list)
    rules: list[ThresholdRule] = field(default_factory=list)
    # DSL 规则默认空：只给 rules 的旧调用点行为逐字节不变。
    dsl_rules: list[DslRuleSpec] = field(default_factory=list)
    generator_provenance: str = ""  # 生成依据的知识来源

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
