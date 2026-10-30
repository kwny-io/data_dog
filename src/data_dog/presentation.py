"""展示层派生：把「候选 + 统计 + 遥测」凝练成一句人话结论（verdict）。

为什么要抽出来
--------------
`scenarios.ScenarioRun.verdict` / `verdict_detail` 原本是内联在演示场景里的逻辑，
只有 Streamlit 演示台在用。阶段 D 的 HTTP 端点（`GET /v1/candidates/{id}`）也要给
前端同样的「一句话结论」——若各写一份，两边迟早分叉：演示台说"过温"，前端说
"无异常"。这里把判定逻辑收敛到**唯一实现**，`scenarios` 改为**委托**调用它
（因此 `ui/app.py` 的 `run.verdict` 行为不变、零改动）。

输入约定
--------
- `candidates`：至少含一个 dict，键有 `severity_hint` / `component_ref` /
  `rule_matches`（与 `CandidateEvent.to_dict()` 一致）。
- `stats`：数据面统计（`rejected` / `rejected_by_reason` 等），可为空。
- `telemetry`：部件 → `{"metrics": {...}}`，用于从证据里取温度。
"""
from __future__ import annotations

from typing import Any

from .go2_sim.semantic import COMPONENT_KIND_NAME, COMPONENT_PART_NAME, signal_name

_SEVERITY_CN = {"critical": "严重", "warning": "警告"}

# ------------------------------------------------- 信号族 → 结论里的半句

# 调查结论里"具体是什么越线了"这半句，**不能从 `signal_name` 拼出来**。
# 两者语法角色不同：卡片标签要短（"温度过高"、"电量"），是名词短语；
# 句子里要的是一个能直接接在部件名后面的**完整谓语**。硬用标签拼会得到
# "主机电池电量，低于告警线"（多一个逗号）这种半成品。
#
# 更要紧的是**方向**：温度越上限、电量破下限、电压也破下限——
# 一个"越过阈值"套不住两头。套错方向比说错指标名更危险，
# 值班员会照着反方向去排查。所以按族逐个写死谓语。
#
# ⚠ 这张表**不镜像到 web**：结论正文由服务端组好再发给前端，
#   web 侧不拼句子，多一份镜像只会多一处漂移点。
#   但新增信号族时必须同步补这里，否则结论会退化成
#   "X出现异常"这种既不具体也不说谎的兜底句（见 `signal_clause`）。
#
# 放在 `presentation` 而不是 `go2_sim/semantic`：语义层负责"叫什么"，
# 组句是展示层的活。且 `providers` 不能依赖 `go2_sim`
# （会形成 providers→go2_sim→mujoco_sim→providers 的环，
# 由 tests/test_layering.py 守着），而这里已经是名字翻译的汇聚点。
SIGNAL_CLAUSE: dict[str, str] = {
    # 越上限
    "temperature_high": "温度越过厂商保护阈值",
    "temperature": "温度越过厂商保护阈值",
    "current": "电流越过厂商保护阈值",
    "cell_v_spread": "单体压差超过均衡阈值",
    # 趋势 / 变化率
    "temperature_rising": "温度较基线持续上升",
    "lost": "失联计数持续上涨",
    "tau_est": "估计力矩持续偏离预期",
    # 说是"角度"不是"关节角度"：部件名本身以"关节电机"结尾，
    # 再写"关节角度"会读成"左前膝关节电机**关节**角度出现突跳"。
    "q": "角度出现突跳",
    # 破下限
    "soc": "电量低于告警线",
    "bms_soc": "电量低于告警线",
    "power_v": "母线电压低于安全下限",
    "fan_frequency": "风扇转速低于下限",
    "bandwidth": "通信带宽低于保障值",
    "body_height": "机身高度低于安全下限",
    # 布尔量
    "current_anomaly": "电流超出正常范围",
    "fault_flag": "故障标志位持续置位",
}


def signal_clause(signal: str) -> str:
    """`soc` → "电量低于告警线"，接在部件名后面就是一整句。

    表里没有的信号族**不猜方向**：兜底句说"X出现异常"，
    不编造它是越上限还是破下限。宁可说得少，也不给值班员一句错的话。

    这条函数之所以存在，是因为原实现把"温度越过厂商保护阈值"
    写死在结论模板里，于是 `battery.main` 成了
    "主机电池温度越过厂商保护阈值"——电池报的是 SOC/电压/压差（台账 A86）。
    """
    hit = SIGNAL_CLAUSE.get(signal)
    if hit is not None:
        return hit
    label = signal_name(signal) if signal else ""
    if label:
        return f"{label}出现异常"
    return "出现异常"


def component_cn(component_ref: str) -> str:
    """部件显示名：能翻就翻，翻不了**原样返回**（不编造名字）。

    这里原来有一份 6 条的 `_COMPONENT_CN` 硬编码表，只覆盖左前髋/左前膝等
    少数部件，且与 `go2_sim/semantic.py` 的表**各自演进**——同一部件在两个
    页面上叫法不同，没登记的部件则直接漏英文。

    现在改为拼 `semantic.py` 的两张表（部件类型 + 部件实例），覆盖全部 12 个
    关节和 5 类部件。**保留"翻不了就原样返回"这个语义**：调用方（演示台的
    一句话结论、HTTP 调查端点）需要的是"宁可露出原串，也不要编一个中文名"，
    所以不做 `semantic.component_name` 那种拆词重组。
    """
    if not component_ref:
        return component_ref
    kind_raw, sep, part_raw = component_ref.partition(".")
    if not sep:
        return COMPONENT_KIND_NAME.get(kind_raw, component_ref)
    kind = COMPONENT_KIND_NAME.get(kind_raw)
    part = COMPONENT_PART_NAME.get(part_raw)
    if kind is None or part is None:
        return component_ref          # 任一半没登记 → 原样，不拼半个中文
    return f"{part}{kind}"


# -------------------------------------- 信号族 → 根因假设 / 处置建议
#
# 这两张表跟 `SIGNAL_CLAUSE` 是同一个缺陷（A86）的另外两个面：
# 结论说对了指标，但根因写成"热退化/冷却失效"、建议写成"检查冷却通道"，
# 信号是电量时照样把值班员支到不存在的方向上。**比不给建议更糟**，
# 因为他会照做。
#
# 没有对应条目时给通用建议，不套热故障剧本。
_CONFIRMED_CAUSE: dict[str, str] = {
    "temperature_high": "热退化/冷却失效",
    "temperature": "热退化/冷却失效",
    "temperature_rising": "散热能力下降",
    "current_anomaly": "负载异常或驱动侧故障",
    "current": "负载异常或驱动侧故障",
    "tau_est": "力矩估算偏差或机械卡滞",
    "q": "机械干涉或编码器跳变",
    "lost": "通信线束松动或收发器故障",
    "soc": "电池耗尽或供电路径异常",
    "bms_soc": "电池耗尽或供电路径异常",
    "power_v": "母线供电异常",
    "cell_v_spread": "单体老化或均衡失效",
    "fan_frequency": "风扇故障或风道堵塞",
    "bandwidth": "链路拥塞或线束接触不良",
    "body_height": "姿态异常或支撑失效",
    "fault_flag": "部件自检报错",
}

_CONFIRMED_ACTIONS: dict[str, list[str]] = {
    "temperature_high": ["降低负载并安排检修", "检查冷却通道"],
    "temperature": ["降低负载并安排检修", "检查冷却通道"],
    "temperature_rising": ["复查散热与负载工况", "安排温升趋势复核"],
    "current_anomaly": ["检查驱动电流回路", "安排负载侧排查"],
    "current": ["检查驱动电流回路", "安排负载侧排查"],
    "tau_est": ["复核力矩标定与零点", "检查机械连杆与轴承"],
    "q": ["检查机械干涉与限位", "复核编码器与线束"],
    "lost": ["检查通信线束与接头", "复核收发器与供电"],
    "soc": ["安排充电或更换电池", "核查供电路径与功耗"],
    "bms_soc": ["安排充电或更换电池", "核查供电路径与功耗"],
    "power_v": ["检查母线供电与线束", "复核电源模块"],
    "cell_v_spread": ["检查单体均衡与老化", "安排电池包检测"],
    "fan_frequency": ["检查风扇与风道", "安排散热检修"],
    "bandwidth": ["检查通信链路与接头", "复核带宽占用"],
    "body_height": ["检查支撑结构", "安排姿态标定"],
    "fault_flag": ["按部件手册执行自检", "安排现场检查"],
}


def confirmed_cause(signal: str) -> str:
    """确认结论的根因假设。表里没有 → 说"该信号持续异常"，不猜机理。"""
    return _CONFIRMED_CAUSE.get(signal, f"{signal_name(signal) or '该信号'}持续异常")


def confirmed_actions(signal: str) -> list[str]:
    """确认结论的处置建议。表里没有 → 通用现场检查，不套热故障剧本。"""
    return list(_CONFIRMED_ACTIONS.get(signal, ["安排现场检查并核对传感器"]))


def _first_temperature(telemetry: dict[str, dict[str, Any]]) -> float | None:
    for ev in telemetry.values():
        t = (ev or {}).get("metrics", {}).get("temperature_c")
        if t is not None:
            return float(t)
    return None


def derive_verdict(
    candidates: list[dict[str, Any]],
    stats: dict[str, Any] | None,
    telemetry: dict[str, dict[str, Any]] | None,
) -> tuple[str, str]:
    """返回 `(verdict, verdict_detail)`：一句话结论 + 一句人话解释。

    规则（与 `ScenarioRun` 原实现一致）：
      1. 有候选 → 按最高严重度给"检测到 N 条 X 异常"。
      2. 无候选但数据面拒绝过 → "日志被拒绝（失败关闭）"。
      3. 无候选且解析不出指标 → 说明"字段与算子对不上"。
      4. 其余 → "没有检测到异常"。
    """
    stats = stats or {}
    telemetry = telemetry or {}

    if candidates:
        c = candidates[0]
        sev = _SEVERITY_CN.get(c.get("severity_hint", ""), c.get("severity_hint", ""))
        verdict = f"⚠️ 检测到 {len(candidates)} 条{sev}异常"
        name = component_cn(c.get("component_ref", ""))
        temp = _first_temperature(telemetry)
        if "MOTOR_TEMP_PROTECT" in (c.get("rule_matches") or []) and temp is not None:
            detail = f"{name}温度 {temp:.0f}°C，超过厂商文档写明的 90°C 安全保护线。"
        else:
            detail = f"{name}命中了检测规则：{', '.join(c.get('rule_matches') or [])}。"
        return verdict, detail

    if stats.get("rejected_by_reason") or stats.get("rejected"):
        return ("⛔ 日志被拒绝（失败关闭）",
                "设备信息与已知兼容范围对不上，数据面拒绝处理而不是猜测。")

    has_metrics = any((ev or {}).get("metrics") for ev in telemetry.values())
    if not has_metrics:
        return ("✅ 没有检测到异常",
                "这条日志的字段和当前检测算子对不上（本算子只覆盖关节电机），"
                "解析不出可用指标，所以没有可判定的内容。")
    return ("✅ 没有检测到异常",
            "各项读数都在厂商文档给出的规则范围内，没有触发任何检测规则。")
