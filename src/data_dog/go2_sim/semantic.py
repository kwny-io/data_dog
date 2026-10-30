"""人话名：把三套标识符翻译成值班员能直接念出来的中文。

## 为什么要有这一层

系统里同时在用**四套互不相干的标识符**，每一套都会漏到界面上：

| 来源 | 长相 | 谁产生 |
|---|---|---|
| 故障键 | `F1_stall` | `faults.py` 的 `FAULT_LIBRARY` |
| 规则 ID | `MOTOR_STALL_SUSTAINED` | 各 `ThresholdRule.rule_id` |
| 派生事件类型 | `joint_motor_temperature_rising_under_normalized_load` | `faults.py` 的 `label`、`emit.py` 的 `component_hint` |
| 执行器名 | `FL_thigh` | `rig.py` 的 `ACTUATORS`（照抄 MuJoCo/Unitree） |

**它们都是给机器看的**。直接渲染出来，界面上就是
`joint_motor.front_left_hip` 和 `POWER_SOC_LOW` —— 值班员要么不认识，
要么得在现场心算一遍。实测（2026-10-30）仿真控制台的候选卡片
就是这么显示的第一行。

第 4 套（执行器名）是 2026-10-31 补的：关节遥测表原来直接印 `FL_hip`，
用户当场反馈"别人看不懂"。它**没有单独的翻译表**——`_JOINT_TO_COMPONENT`
已经把执行器映射到部件，用 `component_part_name()` 取部件那一段就是人话名。
再抄一张表就是两个事实源，改一处忘一处。

## 为什么不全靠逐词拼接

`web/lib/vocabulary.ts` 有一套逐词翻译（`temperature_rising_under_normalized_load`
→ "温度持续上升欠正常化负载"），思路对，但**它不认识业务词**：
`stall`/`slip`/`gait`/`encoder`/`tau`/`bias` 全都不在词表里，
拼出来就是混着英文的怪句子。逐词只能兜底，**准确的名字必须显式写死**。

所以这里的策略是：**显式表优先，逐词兜底**。
表里没有的标识符不会崩，也不会假装认识——它会把原串原样交回去，
由调用方决定降级怎么显示。

## 这个模块不做的事

- **不改任何线上标识符**。`key`/`rule_id`/`event_type` 该怎么传还怎么传，
  这层只负责"显示成什么"。
- **不引入 web 依赖**。表是纯数据，web 侧有一份对应的 TS 视图，
  两边由同一个测试卡着（见 `tests/test_go2_sim_semantic.py` 的
  `test_web_and_python_agree`）。
"""
from __future__ import annotations

from .faults import FAULT_LIBRARY

# --------------------------------------------------------------- 故障名

# 从 FAULT_LIBRARY 直接取 `label`——**不在这里重抄一遍**。
# 抄一遍就有两个事实源，改了一处忘另一处，正是本项目栽过好几次的坑
# （见 21 号台账 A81：文档表格里的规则名和代码里的对不上）。
FAULT_NAME: dict[str, str] = {f.key: f.label for f in FAULT_LIBRARY}

# 一句话故事。给 tooltip 用——名字只有四个字，说不出"腿被台阶卡住"这种现场。
FAULT_STORY: dict[str, str] = {f.key: f.story for f in FAULT_LIBRARY}

# 症状。给"这条为什么报"用。
FAULT_SYMPTOM: dict[str, str] = {f.key: f.symptom for f in FAULT_LIBRARY}


# --------------------------------------------------------------- 规则名

# 全部规则 ID。**两个来源，都要收**（2026-10-30 逐条核对得来）：
#
#   1. 仿真器 23 条 —— 从全量 24 例矩阵的实测输出里抄的，不是从代码里猜的；
#   2. 真算子 3 条 —— `operators/generated/*.yaml` 里的 `rule_id`，
#      由 `scripts/generate_operators.py` 生成，走真链路时会进候选卡片。
#
# 第 2 类是补表时才发现的：原来只照着仿真器抄，页面上一换到真数据就
# 冒出 `BATT_SOC_LOW`。**只统计一个来源 = 只闭了一半的口**。
# 新增规则必须同时补进这张表，否则界面上又会冒出下划线串。
#
# 命名原则：**说现象，不说机制**。值班员要的是"电机堵转了"，
# 不是"STALL_SUSTAINED 条件在窗口内持续成立"。
RULE_NAME: dict[str, str] = {
    # ---- 电机（data_plane 的电机算子）
    "MOTOR_STALL_SUSTAINED": "电机持续堵转",
    "MOTOR_LOAD_SPIKE": "电机负载骤增",
    "MOTOR_TEMP_PROTECT": "电机过温保护",
    "MOTOR_TEMP_DEGRADE": "电机高温降额",
    "MOTOR_TEMP_RISE_UNDER_LOAD": "负载下温度上升",
    "MOTOR_ENCODER_JUMP": "编码器跳变",
    "MOTOR_TAU_EST_FAIL": "力矩估算失效",
    "MOTOR_LOST_COUNTER_RISING": "电机失联计数上涨",
    "MOTOR_CURRENT_BIAS": "电流零点漂移",
    "MOTOR_MODE_FLAG_SET": "电机模式位异常",
    # ---- 电源 / 电池
    "POWER_SOC_LOW": "电量过低",
    "POWER_BUS_OVERCURRENT": "母线过流",
    "POWER_BUS_VOLTAGE_LOW": "母线电压过低",
    "BATTERY_CELL_IMBALANCE": "单体电压不均衡",
    "BATTERY_OVERHEAT": "电池过温",
    # ---- 通信
    "COMM_PACKET_LOSS": "通信丢包",
    "COMM_FIELD_INCOMPLETE": "报文缺字段",
    "COM_CRC_ERROR_RATE": "CRC 错误率偏高",
    "COM_BANDWIDTH_LOW": "带宽不足",
    # ---- 热管理 / 整机
    "COM_FAN_STALLED": "风扇停转",
    "POSTURE_FALLEN": "机身翻倒",
    "SYS_ERROR_CODE_SET": "整机报错",
    # ---- 数据质量
    "FIELD_SANITY_TEMP_INVALID": "温度字段越界",
    # ---- 真算子（operators/generated/*.yaml，见文件头说明）
    "MOTOR_TEMP_TREND": "电机热退化风险",
    "MOTOR_TEMP_OVERPROTECT": "电机过温停机",
    "BATT_SOC_LOW": "电池电量过低",
    # ---- 演示夹具与评测样本（evidence/candidates/*.json、evals/cases.py）
    # 这几条不进仿真器，但会随演示/回放数据出现在界面上，
    # 不登记就又是下划线串。
    "MOTOR_TEMP_TREND_V2": "电机热退化风险（V2 阈值）",
    "BATT_VOLT_TREND": "电池电压趋势异常",
    "SPURIOUS_CHECK": "误报复核",
    "LOW_CONF": "低置信度",
    "MANUAL_IMPORT": "人工导入",
    "UNKNOWN": "未识别规则",
}


# --------------------------------------------------------------- 信号族

# 候选卡片上的 `signal` **不是**事件类型，也不是规则 ID，而是
# `data_plane/candidate.py::_signal_of(rule_id, field)` 派生出来的**信号族**。
# 它优先按 rule_id 里的词根归类，归不进去就**原样返回字段名**——
# 于是 `tau_est` / `q` / `lost` / `soc` 这些裸字段名直接漏到界面上。
# 实测（2026-10-30）控制台第一张候选卡就是 `tau_est`。
#
# 全部取值是下面 14 个，来自两处算子装配：
#   - `operator_factory/p1_demo.py::_RULES_BY_SUBSYSTEM`（11 条电机/通信规则）
#   - `go2_sim/subsystem_operator.py::build_subsystem_operator`（其余子系统）
# 两边合起来去重后就是这 14 个，没有第三个来源。
#
# ⚠ 这张表按 **signal 的字面值**索引，不是 rule_id。把规则的
# `rule_matches` 改成 `_signal_of` 的返回值去查，查的是另一套键。
SIGNAL_NAME: dict[str, str] = {
    # `_signal_of` 的四个归类结果（有语义，不是字段名）
    "temperature_high": "温度过高",
    "temperature_rising": "温度上升",
    "current_anomaly": "电流异常",
    "fault_flag": "故障标志位",
    # 归不进去时 `_signal_of` 的兜底——返回的字段名，下面逐个给名
    "temperature": "温度",
    "current": "电流",
    "tau_est": "估计力矩",
    "q": "关节角度",
    "lost": "失联计数",
    # 同一个 `POWER_SOC_LOW` 在两处声明了**不同的 field**：
    # 仿真器 `subsystem_operator.py` 用载荷键 `soc`，
    # 真链路 `p1_demo.py` 用 IDL 映射后的标准名 `bms_soc`。
    # 两个值都会出现在候选卡片上，**都要登记**——只登记 `soc` 时，
    # 真链路的候选显示成 "bms soc"（实测，2026-10-30）。
    "soc": "电量",
    "bms_soc": "电量",
    "power_v": "母线电压",
    "cell_v_spread": "单体压差",
    "fan_frequency": "风扇转速",
    "bandwidth": "通信带宽",
    "body_height": "机身高度",
}


# --------------------------------------------------------------- 事件类型

# 事件类型是"部件类型 + 信号"拼出来的（见 providers/offline_demo.py 的
# `_event_type`），所以理论上是个开放集合。但这几组**语义重复、说法不同**
# 的组合值得写死——它们在演示和真实链路里反复出现。
EVENT_TYPE_NAME: dict[str, str] = {
    "motor_over_temperature": "电机过温",
    "joint_motor_thermal_degradation": "关节电机热退化",
    "joint_motor_temperature_rising_under_normalized_load":
        "负载未变但温度上升",
    "battery_over_discharge": "电池过放",
    "battery_cell_imbalance": "电池单体不均衡",
    "communication_packet_loss": "通信丢包",
    "unknown_event": "未识别事件",
}


# ----------------------------------------------------------- 部件实例名

# 部件实例（`joint_motor.front_left_hip` 的那一段）。
# 与 web 的 `COMPONENT_PART` 同源；这里是 Python 侧的唯一事实源。
COMPONENT_PART_NAME: dict[str, str] = {
    "front_left_hip": "左前髋",
    "front_right_hip": "右前髋",
    "rear_left_hip": "左后髋",
    "rear_right_hip": "右后髋",
    "front_left_knee": "左前膝",
    "front_right_knee": "右前膝",
    "rear_left_knee": "左后膝",
    "rear_right_knee": "右后膝",
    # 小腿段（`*_calf`）。**必须先于 foot 之类补齐**：漏了它时
    # `component_name("joint_motor.front_left_calf")` 会拼出
    # "front left calf 关节电机" —— 中英混排，正是这层要消灭的东西。
    # 实测 2026-10-31：12 个关节里 4 个 calf 全中。
    "front_left_calf": "左前小腿",
    "front_right_calf": "右前小腿",
    "rear_left_calf": "左后小腿",
    "rear_right_calf": "右后小腿",
    "front_left_foot": "左前足",
    "front_right_foot": "右前足",
    "rear_left_foot": "左后足",
    "rear_right_foot": "右后足",
    "main": "主机",
    "bms": "BMS",
}

# 部件类型（`joint_motor` 的那一段）。
COMPONENT_KIND_NAME: dict[str, str] = {
    "joint_motor": "关节电机",
    "motor": "电机",
    "battery": "电池",
    "bms": "BMS",
    "controller": "主控",
    "imu": "IMU",
    "lidar": "激光雷达",
    "camera": "相机",
    "main": "主机",
}


# --------------------------------------------------------------- 公开 API

def fault_name(key: str) -> str:
    """`F1_stall` → "堵转"。未知 key 原样返回，不抛错。

    返回原串而不是 "未知故障"，是因为**显示层不该吞信息**：
    真出了没登记的名字，界面上看见 `F99_foo` 比看见"未知故障"有用得多——
    前者能直接拿去 grep，后者只能让人猜。
    """
    return FAULT_NAME.get(key, key)


def rule_name(rule_id: str) -> str:
    """`MOTOR_STALL_SUSTAINED` → "电机持续堵转"。

    表里没有时退化成"去掉下划线、首字母大写"，**不保留全大写蛇形串**——
    那是日志里的长相，不是给人念的。真需要原始串的地方（工单、grep）
    由调用方自己留着一份 `rule_id`。
    """
    hit = RULE_NAME.get(rule_id)
    if hit is not None:
        return hit
    if not rule_id:
        return rule_id
    return rule_id.replace("_", " ").strip().capitalize()


def signal_name(signal: str) -> str:
    """`tau_est` → "估计力矩"，`temperature_high` → "温度过高"。

    这是**候选卡片第一行**用的那套名字。未知值退化成去下划线，
    与 `rule_name` 同一套兜底——但注意 signal 集合很小且封闭，
    兜底基本只是防御，正常路径应该全部命中。
    """
    hit = SIGNAL_NAME.get(signal)
    if hit is not None:
        return hit
    if not signal:
        return signal
    return signal.replace("_", " ").strip()


def event_type_name(event_type: str) -> str:
    """`motor_over_temperature` → "电机过温"。

    未知类型退化成逐词可读化（下划线换空格），不做逐词翻译——
    那需要词表，而词表的维护成本应该留在 web 侧一处。
    """
    hit = EVENT_TYPE_NAME.get(event_type)
    if hit is not None:
        return hit
    if not event_type:
        return event_type
    return event_type.replace("_", " ").strip()


def component_name(ref: str) -> str:
    """`joint_motor.front_left_hip` → "左前髋关节电机"。

    实例名在前、类型名在后——中文习惯说"左前髋关节电机"，
    不说"关节电机的左前髋"。
    """
    if not ref:
        return ref
    kind_raw, _, part_raw = ref.partition(".")
    kind = COMPONENT_KIND_NAME.get(kind_raw)
    part = COMPONENT_PART_NAME.get(part_raw)
    # 两边都查到了就直接拼（中文拼中文，不用空格）。
    # 任一边是兜底出来的英文，中间加空格——否则会粘成
    # "unknown joint关节电机" 这种既不是中文也不是英文的串。
    kind_known = kind is not None
    part_known = part is not None
    kind = kind if kind_known else (kind_raw.replace("_", " ") or "")
    part = part if part_known else part_raw.replace("_", " ")
    if kind and part:
        sep = "" if (kind_known and part_known) else " "
        return f"{part}{sep}{kind}"
    return part or kind or ref


def component_part_name(ref: str) -> str:
    """`joint_motor.front_left_hip` → "左前髋"：只要部件那一段，丢掉类型。

    关节遥测表要的是这个。那张表 12 行全是关节电机，行行写"……关节电机"
    只是每行多占四个字——类型由表头交代一次就够。

    未知部件退化成去下划线，不做逐词翻译；空串原样返回。
    """
    if not ref:
        return ref
    _, _, part_raw = ref.partition(".")
    hit = COMPONENT_PART_NAME.get(part_raw)
    if hit is not None:
        return hit
    return part_raw.replace("_", " ") or ref


def describe_fault(key: str) -> dict[str, str]:
    """故障的完整人话描述，给 UI 的 tooltip 用。"""
    return {
        "key": key,
        "name": fault_name(key),
        "story": FAULT_STORY.get(key, ""),
        "symptom": FAULT_SYMPTOM.get(key, ""),
    }


__all__ = [
    "FAULT_NAME", "FAULT_STORY", "FAULT_SYMPTOM",
    "RULE_NAME", "SIGNAL_NAME", "EVENT_TYPE_NAME",
    "COMPONENT_KIND_NAME", "COMPONENT_PART_NAME",
    "fault_name", "rule_name", "signal_name", "event_type_name",
    "component_name", "component_part_name", "describe_fault",
]
