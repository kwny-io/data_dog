"""非电机子系统的检测算子：电池 / IMU / 散热 / 整机。

为什么单独一个模块、而不是并进 `operator_factory/p1_demo.py`
------------------------------------------------------------
`p1_demo.build_motor_operator()` 是**从厂商文档生成**电机算子（字段来自 IDL、
阈值来自协议头），它是"文档→算子"那条线的产物。这里的算子不是从文档生成的，
而是**给仿真演示补的判据**——字段确实照 IDL 命名（见 `emit.payload_for_subsystem`），
但阈值全是项目假设（厂商没写"电量多少算低"）。两条线性质不同，混在一个函数里
会让 provenance 说假话。

**为什么不注册成多个 OperatorBinding**
--------------------------------------
`CompatibilityResolver.resolve()` 只按 vendor/model/firmware/protocol 匹配，
**不看部件**；匹配到 2 个就 `ambiguous` 失败关闭（`resolver.py:94`）。
这是经过测试钉住的有意语义（`test_resolver_unique_no_match_ambiguous`），
不能为了"多部件"去改。所以正确做法是**一个绑定带全部字段**：每条信封由
`component_hint` 决定 `component_ref`，规则只对载荷里实际存在的字段求值
（`Detector.detect` 会跳过 `rule.field not in metrics`）。
实测：3 个部件各自正确路由到自己的候选，互不串台。

阈值出处（一律**项目假设**，不要冒充厂商规格）
----------------------------------------------
| 规则 | 阈值 | 依据 |
|---|---|---|
| `POWER_SOC_LOW` | soc ≤ 15 | 项目假设（IDL 只定义字段，不定义"低电量"） |
| `POWER_BUS_OVERCURRENT` | power_a ≥ 30 | 项目假设：Go2 整机持续功率数百瓦，21.6V 下十几安 |
| `BATTERY_CELL_IMBALANCE` | cell_v_spread ≥ 0.05 | 项目假设：6S 包正常压差在毫伏级 |
| `BATTERY_OVERHEAT` | bq_ntc ≥ 55 | 项目假设 |
| `POWER_BUS_VOLTAGE_LOW` | power_v < 22 | 实测：正常 25.04，奔跑负载最低 23.54；F14 掉到 18.69~20.03 |
| `COM_FAN_STALLED` | fan_frequency ≤ 0.01 | 项目假设：转速归一化 0..1 |
| `COM_CRC_ERROR_RATE` | crc_fail_rate ≥ 0.01 | 项目假设 |
| `COM_BANDWIDTH_LOW` | bandwidth < 500 | 实测：正常恒 1000，F20 恒 340（离散两档） |
| ~~`IMU_GYRO_BIAS`~~ | ~~gyro_x ≥ 0.05~~ | **已删**：正常奔跑 −0.86~+0.81，判不了 |
| ~~`IMU_ACCEL_HIGH/LOW`~~ | ~~accel_norm 偏离 1g~~ | **已删**：正常奔跑 2.80~17.55，判不了 |
| `POSTURE_FALLEN` | body_height < 0.10 | 物理：正常站立 0.25，实测躺平 0.058 |
| `GAIT_STUCK` | gait_type ≤ 0 且 progress ≤ 0 | 项目假设：建了步态却没在走 |

**摔倒为什么用机身高度，不用倾角**：`rig.py` 的 F18 给机身施加横向冲量，实测
20 秒后 `body_height` 从 0.254 掉到 0.058、`roll` 翻到 −3.14 弧度。倾角在翻倒
瞬间会**扫过全范围**（−3.14 和 +0.1 都可能出现），拿阈值判会漏；机身高度在
"站着"和"躺着"之间是单调且无歧义的。这在演示里更好讲。

没实现的三条（F16 陀螺零偏 / F17 加速度计失效 / F21 步态异常）
--------------------------------------------------------------
三条都需要**窗口均值 + 姿态门控**，而本分支的 `Detector.detect(metrics)`
只收单帧 `metrics`，`DetectorConfig` 也没有 `dsl_rules`（窗口类规则由
`operator_factory.dsl.Evaluator` 执行，`pipeline.py` 目前没把
`self.window.features(key)` 传下去）。

单帧阈值在这三条上是**物理上不成立**的。这不是推理，是实测——
曾经写过 F16/F17 的单帧阈值，跑 `normal` 就出了误报，量完数据才明白：

| 量 | 静止（正常） | 奔跑（正常） | 故障读数 |
|---|---|---|---|
| `accel_norm` | 9.72 ~ 9.90 | **2.80 ~ 17.55** | F17 站立 6.884 |
| `gyro_x` | −0.003 ~ +0.005 | **−0.86 ~ +0.81** | F16 站立 0.087 ~ 0.095 |
| `gyro_y` | −0.003 ~ +0.004 | **−1.08 ~ +1.29** | F16 站立 0.051 ~ 0.058 |
| `gait_type` | 0（站立/趴卧均正常） | 1 | F21 也是 0 |

前两行是同一个病：**正常奔跑的瞬时摆动比故障偏移大一个数量级**。
`gyro_x` 的故障偏移 0.09 rad/s 只有正常奔跑峰值的 1/10，拿 0.05 当阈值，
正常奔跑每一帧都超——这正是冒烟测试里 `normal` 出候选的原因。

正确判据不是"读数大不大"，而是"**静止时**读数该是多少"：
真机的陀螺和加速度计都是静止标定的。所以需要
（a）窗口均值把步态冲击平滑掉，（b）一个"确实在静止"的门控
（`gait_type == 0` 且窗口内 `gyro` 方差足够小）。

F21 同理：`gait_type=0` 在站立和趴卧时都是**正常**的，
单看它会把整机待机判成故障；必须配 `progress` 的窗口均值才能表达
"声明 idle 却在走"。

补齐方式：把 `pipeline.process_one` 里已经算好的窗口特征传给 `detect()`
（检测层分支正在做），然后按 `window.mean(...)` + 门控写这三条。
在那之前，它们留在"待补清单"里，**不伪造成能跑的样子**。
"""
from __future__ import annotations

from ..schemas.operator_config import DetectorConfig, FieldSpec, ThresholdRule

# 声明字段。**type 取 IDL 类型**（不是载荷实际发的 float）：
# `Parser._coerce` 只做 int()/float()，浮点字段按 IDL 取整会丢精度，
# 所以这里跟载荷保持一致用 float32（见 emit.py 的说明：两处类型不符已明写）。
_FIELDS: tuple[tuple[str, str], ...] = (
    # 电池（BmsState + LowState 顶层母线量）
    ("soc", "uint8"),
    ("status", "uint8"),
    ("cycle", "uint16"),
    ("power_v", "float32"),
    ("power_a", "float32"),
    ("bq_ntc", "float32"),
    ("mcu_ntc", "float32"),
    ("cell_v_max", "float32"),
    ("cell_v_min", "float32"),
    ("cell_v_spread", "float32"),
    # IMU（IMUState 展平后的标量）
    ("roll", "float32"),
    ("pitch", "float32"),
    ("yaw", "float32"),
    ("gyro_x", "float32"),
    ("gyro_y", "float32"),
    ("gyro_z", "float32"),
    ("accel_x", "float32"),
    ("accel_y", "float32"),
    ("accel_z", "float32"),
    ("accel_norm", "float32"),
    ("imu_temp", "float32"),
    # 散热 / 通信（LowState 顶层）
    ("fan_frequency", "float32"),
    ("temperature_ntc1", "float32"),
    ("bandwidth", "uint16"),
    ("crc_fail_rate", "float32"),
    # 整机 / 步态（SportModeState）
    ("error_code", "int32"),
    ("gait_type", "int32"),
    ("body_height", "float32"),
    ("progress", "float32"),
    ("foot_raise_height", "float32"),
    ("foot_slip_max", "float32"),
    ("foot_speed_fl", "float32"),
    ("foot_speed_fr", "float32"),
    ("foot_speed_rl", "float32"),
    ("foot_speed_rr", "float32"),
    ("foot_force_fl", "float32"),
    ("foot_force_fr", "float32"),
    ("foot_force_rl", "float32"),
    ("foot_force_rr", "float32"),
    ("yaw_speed", "float32"),
)

_ASSUMPTION = "项目假设（厂商文档只定义字段，不定义该阈值）"


def build_subsystem_operator() -> DetectorConfig:
    """装配非电机子系统的检测算子（全部 `optional=True`）。

    **全部 optional 是必须的**：一条电机信封里没有 `soc`，一条 IMU 信封里没有
    `power_a`。声明成必填会让每条信封都带一个 `__missing__` 列表，
    `parse_confidence` 因此被压到 0.8（`pipeline.py:153`），故障照报但置信度
    被无谓污染。缺字段在这里是**正常**的，不是解析失败。
    """
    fields = [
        FieldSpec(name=name, source=name, type=typ, optional=True)
        for name, typ in _FIELDS
    ]

    def rule(rule_id: str, field: str, op: str, value: float,
             severity: str, message: str) -> ThresholdRule:
        return ThresholdRule(rule_id=rule_id, field=field, operator=op,
                             value=value, severity_hint=severity,
                             message=message)

    rules = [
        # ---------------------------------------------------------- 电源
        rule("POWER_SOC_LOW", "soc", "le", 15.0, "warning",
             f"电量 ≤15%，需尽快返航充电（{_ASSUMPTION}）"),
        rule("POWER_BUS_OVERCURRENT", "power_a", "ge", 30.0, "critical",
             f"母线电流 ≥30A，超出持续放电能力（{_ASSUMPTION}）"),
        rule("BATTERY_CELL_IMBALANCE", "cell_v_spread", "ge", 0.05, "warning",
             f"单体压差 ≥50mV，某节电芯衰减偏快（{_ASSUMPTION}）"),
        rule("BATTERY_OVERHEAT", "bq_ntc", "ge", 55.0, "critical",
             f"电池包 NTC ≥55°C（{_ASSUMPTION}）"),
        # 母线压降与过流是**两条不同的故障**，不要合并：F15 是电流越限
        # （power_a 大、电压靠内阻压降小幅下垂），F14 是回路电阻大
        # （电压被拉低、电流为维持功率反而抬升）。实测 F14 时 power_a
        # 从 6A 抬到 24A，用 30A 的过流线抓不到；电压则从 25.0V 掉到 18.9V。
        rule("POWER_BUS_VOLTAGE_LOW", "power_v", "lt", 22.0, "critical",
             "母线电压 <22V，回路内阻增大或接触不良（正常 25.0V，"
             "奔跑负载下最低实测 23.54V）"),
        # ---------------------------------------------------------- IMU
        # F16 陀螺零偏 / F17 加速度计失效都**不在这里**，见文件头
        # "没实现的三条"：两者都需要窗口均值 + 静止门控，单帧阈值会
        # 让正常奔跑持续误报（实测数字见那里）。
        # ---------------------------------------------------------- 散热 / 通信
        rule("COM_FAN_STALLED", "fan_frequency", "le", 0.01, "warning",
             "风扇转速为 0，散热已失效（风扇卡死/断线）"),
        rule("COM_CRC_ERROR_RATE", "crc_fail_rate", "ge", 0.01, "warning",
             f"总线 CRC 错误率 ≥1%，链路质量劣化（{_ASSUMPTION}）"),
        # 带宽是**离散两档**（正常恒 1000、故障恒 340），中间没有过渡，
        # 阈值取 500 只是取中点，不是调出来的临界值。实测三个姿态都恒定。
        rule("COM_BANDWIDTH_LOW", "bandwidth", "lt", 500.0, "warning",
             f"采样带宽 <500（正常 1000，故障实测 340），链路负载过高（{_ASSUMPTION}）"),
        # ---------------------------------------------------------- 整机 / 步态
        rule("POSTURE_FALLEN", "body_height", "lt", 0.10, "critical",
             "机身高度低于触地高度，已翻倒（正常站立 0.25m）"),
        rule("SYS_ERROR_CODE_SET", "error_code", "gt", 0.0, "critical",
             "整机上报了非零错误码（厂商错误码表见 LowState/MError 文档）"),
    ]

    # 加速度计失效（F17）与步态异常（F21）**故意不在这里**，原因写到文件头
    # 的"没实现的两条"一节——它们需要窗口均值，本分支的 `Detector` 不收窗口
    # 数据。硬塞一条单帧阈值进去，正常奔跑会一直误报。

    return DetectorConfig(
        operator_id="local.go2-subsystem-detector",
        operator_type="parser+detector",
        # `component` 只是声明默认值：实际 `component_ref` 取信封的
        # `component_hint`（`pipeline._component_of`），所以一个算子能服务四个部件。
        component="battery.main",
        input_schema="raw-log-envelope-v1",
        output_schema="normalized-telemetry-v1",
        fields=fields,
        rules=rules,
        generator_provenance=(
            "字段名对齐厂商 IDL（BmsState / IMUState / LowState / SportModeState）；"
            "全部阈值均为项目假设，无厂商出处。字段物理含义有出处，数值门槛没有。"
        ),
    )


__all__ = ["build_subsystem_operator"]
