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

from ..core.console import ensure_utf8_console
from ..paths import vendor_docs_dir
from .dsl import DSLError
from .knowledge import build_knowledge_package, flag_unconfirmed
from .pipeline import DeclarativeRule, Fixture, Operator, replay_all

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# 窗口类判据的**最小样本门槛**：不足 8 条就不做趋势/占比/丢包判断。
# 这不是调参，是语义：一条样本算不出斜率、算不出"持续占比"、也算不出丢包率。
# 它同时守住"启动首事件不得误报"——既有演示场景都是单事件，因此天然不触发。
MIN_WINDOW_SAMPLES = 8


def cleaned_dir() -> Path:
    """厂商清洗后文档目录（运行时解析，尊重 DD_VENDOR_DOCS 覆盖）。"""
    return vendor_docs_dir() / "unitree" / "cleaned"


def _win(expr: str, *, min_samples: int = MIN_WINDOW_SAMPLES) -> str:
    """给窗口判据套上"样本足够"前置条件（降级语义的唯一入口，别在各处手写）。"""
    return f"window.samples() >= {min_samples} and ({expr})"


def _pick_field(pkg, name: str, prefer: str = ""):
    """按名字取字段规格；同名跨结构体时**必须**按结构体挑，不能取"第一个出现"。

    真实缺陷：`temperature` 同时出现在 IMUState 与 MotorState，`current` 只在
    BmsState。按名字取首个会让电机算子拿 IMU 的温度、电池的电流。
    """
    cands = [f for f in pkg.fields if f.name == name]
    if not cands:
        return None
    for f in cands:
        if f.structure == prefer:
            return f
    return cands[0] if len(cands) == 1 else None


def _field(pkg, name: str, prefer: str) -> dict | None:
    f = _pick_field(pkg, name, prefer)
    if f is None:
        return None
    return {
        "name": f.name, "source": f.name, "type": f.type, "unit": f.unit,
        "optional": False,
        # 结构体必须带出去：下游 FieldSpec 靠它区分"电机温度"与"IMU 温度"。
        "structure": f.structure,
        "provenance": "verified",     # 字段本身在厂商 IDL 里明写
    }


def build_motor_operator(pkg) -> Operator:
    """从知识包装配 front_left_hip 电机检测算子。

    - fields：来自 IDL 字段规格（真实字段，**按结构体挑**，不按名字取首个）
    - rules（声明式）：**只用有权威出处的数值**——电机温度保护阈值 90°C 来自官方开源协议头
      （unitree_actuator_sdk，BSD-3），标记为厂商事实。
    - dsl_rules：多字段窗口 + 位运算。**除 90°C 外没有任何厂商数值阈值**，所以每条阈值型
      判据都显式标 `provenance="assumption"`、message 里写明"项目假设"。

    规则按子系统分组（A 关节/电机、B 热、E 通信、F 整机负载），见 `_RULES_BY_SUBSYSTEM`。
    分不出物理上真正的故障类型时**不硬凑规则**——那些列在报告的"需采集侧补字段"清单里。
    """
    # 电机自身的字段：只从 MotorState 取。同名跨结构体时必须按结构体挑：
    # `temperature` 在 IMUState 里也有，`mode` 在 SportModeState 里也有。
    uniq_fields: list[dict] = []
    for name in ("mode", "temperature", "dq", "tau_est", "lost", "q"):
        f = _field(pkg, name, "MotorState")
        if f is not None:
            uniq_fields.append(f)

    # `current` 是**特例，必须写明白**：厂商 MotorState IDL **没有**电机电流字段
    # （IDL 里唯一的 current 是 BmsState 的电池总线电流，int32/ampere）。
    # 本项目 motor-log-v2 载荷里的 current 是**采集侧派生的每关节电机电流**，
    # 属于项目自定义协议——所以它标 assumption，且不挂任何厂商结构体。
    uniq_fields.append({
        "name": "current", "source": "current", "type": "float32",
        "unit": "ampere", "optional": False, "structure": "",
        "provenance": "assumption",
    })

    # C / 电源：BmsState 的**标量**字段。这些名字在电机载荷里不存在，可以安全并入
    # 同一个算子（数组型字段 bq_ntc / mcu_ntc / cell_vol 进不来——见报告"需采集侧
    # 补字段"：`_coerce` 不展平数组，窗口也只收标量）。
    # **命名冲突（已实测踩到）**：BmsState 的 `current` 是**电池总线电流**，与载荷里
    # 表示每关节电机电流的 `current` 同名不同义。若把总线电流也声明成 source="current"，
    # Parser 会把**电机电流的值**（实测 95A）写进 bms_current——电压/电流类判据会全错。
    # 因此总线电流不在此处映射，列为"需采集侧补字段"：采集侧必须用独立键名
    # （如 `bus_current`）上报，才谈得上判据。
    for src, std in (("soc", "bms_soc"), ("status", "bms_status"),
                     ("cycle", "bms_cycle")):
        f = _pick_field(pkg, src, "BmsState")
        if f is None:
            continue
        uniq_fields.append({
            "name": std, "source": src, "type": f.type, "unit": f.unit,
            "optional": True,      # 电机帧里没有 BMS 字段是正常的，不能因此判缺字段
            "structure": "BmsState", "provenance": "verified",
        })

    # 从知识包取**有出处**的电机温度保护阈值（官方开源协议头）
    protect = next((s for s in pkg.numeric_specs
                    if s.key == "motor_temp_protect_c"), None)
    protect_c = protect.value if protect else None
    provenance = (
        "IDL fields from unitree_sdk2 MotorState IDL (structure-qualified); "
        f"temperature protection threshold {protect_c}°C VERIFIED from "
        "unitree_actuator_sdk/motor_msg_GO-M8010-6.h (BSD-3-Clause, official); "
        "MError bit meanings VERIFIED from the same header; "
        "per-joint motor CURRENT is NOT a MotorState IDL field — it comes from the "
        "project log protocol (IDL only has BmsState bus current) → ASSUMPTION; "
        "ALL other numeric thresholds (75/45/50/90 A, 0.25 °C/s, 0.95 coverage, "
        "1.35x/1.2x/1.5x baseline ratios, 3.0 rad encoder step) are PROJECT "
        "ASSUMPTIONS — no vendor document states any of them"
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
            subsystem="B",
            provenance="assumption",      # uint8 温度技术上可为负的"无意义值"；判据是项目定的
        ),
    ]
    if protect_c is not None:
        rules.append(DeclarativeRule(
            rule_id="MOTOR_TEMP_PROTECT",
            field="temperature", operator="ge", value=float(protect_c),
            severity_hint="critical",
            message=f"电机温度达 {protect_c}°C 触发温度保护（厂商开源协议头，verified）",
            subsystem="B",
            provenance="verified",
        ))

    # C / 电源：电量过低。BmsState.soc 是 uint8 百分比——**字段语义 verified**，
    # 但"多少算低"厂商没写，门槛是项目假设。
    if any(f["name"] == "bms_soc" for f in uniq_fields):
        rules.append(DeclarativeRule(
            rule_id="POWER_SOC_LOW",
            field="bms_soc", operator="le", value=15.0,
            severity_hint="warning",
            message="电池电量 ≤15%，需尽快返航充电（字段 BmsState.soc 见 IDL；门槛为项目假设）",
            subsystem="C",
            provenance="assumption",
        ))

    # ------------------------------------------------------------------ DSL 规则
    # 分组：A=关节/电机，B=热，E=通信，F=整机负载。
    # 窗口类判据一律带 `window.samples() >= 8` 前置：一条样本算不出斜率/占比/丢包率，
    # 不足门槛就**不判**（宁可漏报），而不是拿 1 条样本冒充"窗口"。
    dsl_rules = [
        # ==== 原有两条：**逐字节保留**（改它们就等于改既有规则语义，不是增量）====
        {
            "rule_id": "MOTOR_TEMP_RISE_UNDER_LOAD",
            "subsystem": "B",
            "when": ("window.mean(temperature) >= 75 and "
                     "window.mean(current) <= 1.2 * window.baseline(current)"),
            "severity_hint": "warning",
            "message": "负载未显著上升但温度持续偏高（项目假设阈值 75°C，非厂商明写）",
            "field": "temperature",
            "provenance": "assumption",
        },
        {
            "rule_id": "MOTOR_MODE_FLAG_SET",
            "subsystem": "A",
            "when": "(mode & 0x04) != 0",
            "severity_hint": "critical",
            "message": ("电机 mode 第 2 位置位（MError bit2=编码器故障语义 verified 于协议头；"
                        "本 operator 的 mode 位含义映射为项目假设）"),
            "field": "mode",
            "provenance": "assumption",
        },

        # ==== A / 关节电机：堵转（F1）====
        # 判据用"**持续**饱和占比"而不是峰值：实测峰值在正常与故障间重叠（正常 max≈18A
        # 也有尖峰），而 ≥14.5A 的占空比在堵转是 1.000、正常全部 0.000。
        #
        # ⚠ 阈值从 90A 改为 14.5A，原因是**单位换算错误，不是调参**：
        #    原 90A = 23.7N·m ÷ 0.25，把**关节**输出扭矩除以了**电机轴**侧的 kt，
        #    电流被放大 6.33 倍（减速比）。关节侧正确值是 23.7 ÷ 1.5825 = 14.98A。
        #    这个错误与 `rig.py:59-78` 记录的是同一个——那里修了仿真器，规则没跟着改。
        #
        # ⚠ 必须同时要求"关节不转"：只靠电流饱和，堵转与**重载**在峰值上分不开
        #   （实测 F5 负载骤增峰值 14.7A、F1 堵转 14.98A，只差 0.3A；`max(current)<14.5`
        #   这种硬切线两边都会切错）。而堵转的物理定义本来就是"电流饱和**且**关节不转"，
        #   `dq` 是厂商 IDL 里现成的字段（verified），不必绕道猜电流。
        #   三姿态实测（seed 7，30 帧窗）：F1 的 still 占比恒 1.000、mean|dq| ≤0.008；
        #   奔跑正常 mean|dq|=2.787、奔跑 F5=2.310，全部远超 0.05 阈值。
        #   **单独用 dq≈0 会误判站立/趴卧的正常工况**（关节本来就不动），必须与饱和取合取。
        #
        # ⚠ 必须对 **|dq|** 计数（`count_abs`），不能对 `dq` 直接计数：
        #   `dq` 是有符号角速度，`count(dq, "<", 0.05)` 会把**反向转动**（dq=−2.8 rad/s）
        #   也算成"没转"。实测奔跑正常有符号占比 46.2%、距 50% 门槛只差 3.8 个点，
        #   一旦换步态频率或 seed 就会跨过门槛 → 正常奔跑误报堵转。
        #   取 |dq| 后同一组数据是 0.8%，余量 49 个点。与 `mean_abs`/`baseline_abs` 同理：
        #   判"大小"就必须先去掉符号。
        {
            "rule_id": "MOTOR_STALL_SUSTAINED",
            "subsystem": "A",
            "when": _win("window.span() >= 5.0 and "
                         'window.count(current, ">=", 14.5) >= 0.5 * window.samples() and '
                         'window.count_abs(dq, "<", 0.05) >= 0.5 * window.samples()'),
            "severity_hint": "critical",
            "message": ("电流在 ≥5s 窗口内有一半以上时间贴近堵转电流（项目假设：14.5A≈"
                        "额定扭矩 23.7N·m÷关节侧 kt 1.5825 的 97%；厂商文档未给电流阈值）"),
            "field": "current",
            "provenance": "assumption",
        },

        # ==== B / 热：热退化（F2）====
        # 与 F5 的区别是**负载量级**，不是"温升有无"——两者都会让电流上升（见报告）。
        #
        # ⚠ 电流上限 50A→7.9A：50 是 `50N·m÷...` 时代的量级，旧规则里它同时承担
        #   两件事——「电流在中载区间」和「排除堵转/负载骤增」。换算到关节侧 kt 后，
        #   实测三条 fixture 的 mean(I)：热退化 5.11、负载骤增 12.59、堵转 15.26，
        #   7.9A 正好把二者分开。这个数不是拍的，是量出来的分界点。
        {
            "rule_id": "MOTOR_TEMP_DEGRADE",
            "subsystem": "B",
            "when": _win("window.mean(temperature) >= 40 and "
                         "window.slope(temperature) >= 0.25 and "
                         "window.mean(current) < 7.9"),
            "severity_hint": "warning",
            "message": ("温度持续爬升且电流仍在中载区间（项目假设：40°C / 0.25°C·s⁻¹ / "
                        "7.9A；厂商文档未给热退化判据）"),
            "field": "temperature",
            "provenance": "assumption",
        },

        # ==== E / 通信：丢包（F3，包级）====
        # **必须判包级，不能判字段级。** 这条规则原来写的是
        #     window.coverage(temperature) < 0.95
        # 它对**整包丢失**结构上不可见：包根本没进网关 → `push()` 没被调用 →
        # `sample_count` 也不增加，分子分母一起缩水 → 覆盖率恒为 1.0。
        # go2_sim 实测 40s 里整包丢了 16%（332 帧丢 53 帧），覆盖率仍是 1.0，
        # 规则一次都没命中过。字段级覆盖率只能看见"包到了但字段缺失"，
        # 那和整包丢失是两回事。
        #
        # 改用序列号连续性：`packet_coverage()` = 实到 / 应有。
        # 实测（go2_sim，10Hz 抽稀）：正常工况 0% 空洞，F3 故障窗内 16.0%。
        # 门槛 0.95 保持不变（原来就是这个数，语义没变，只是换了正确的观测口径），
        # 余量 11 个百分点。
        {
            "rule_id": "COMM_PACKET_LOSS",
            "subsystem": "E",
            "when": _win("window.packet_coverage() < 0.95"),
            "severity_hint": "warning",
            "message": ("窗口内整包到达率低于 95%（按设备序列号空洞统计；"
                        "项目假设阈值，厂商未给丢包容忍度）"),
            "field": "temperature",
            "provenance": "assumption",
        },

        # ==== E / 通信：丢包（F3，字段级）====
        # 与上面那条**不是重复**：这条看的是"包到了、但有字段没解出来"
        # （载荷残缺 / 解析失败 / 部分字段 NaN），包级那条看不见这种。
        # 两者互补：整包丢失只有包级能看见，字段残缺只有字段级能看见。
        {
            "rule_id": "COMM_FIELD_INCOMPLETE",
            "subsystem": "E",
            "when": _win("window.coverage(temperature) < 0.95 and "
                         "window.packet_coverage() >= 0.95"),
            "severity_hint": "warning",
            "message": ("包都到了，但窗口内温度字段有效率低于 95%"
                        "（载荷残缺或解析失败；项目假设阈值）"),
            "field": "temperature",
            "provenance": "assumption",
        },

        # ==== E / 通信：丢包（F3，固件侧）====
        # `lost` 是 MotorState 里的丢包计数器（IDL 字段 verified）。
        # "增长即异常"是定义性的弱判据 → 仍按项目假设标注，不冒充厂商阈值。
        {
            "rule_id": "MOTOR_LOST_COUNTER_RISING",
            "subsystem": "E",
            "when": _win("window.delta(lost) > 0"),
            "severity_hint": "warning",
            "message": ("电机 lost 丢包计数器在窗口内增长（字段语义 verified；"
                        "'增长即异常'判据为项目假设）"),
            "field": "lost",
            "provenance": "assumption",
        },

        # ==== A / 关节电机：电流零漂/偏置（F4）====
        # 关键区分量是 **tau_est 不跟着涨**：实测 I/base=1.46 而 tau/base=1.07，
        # 真实负载（F5：5.56/5.67、F2：2.94/2.99）两者同步。
        {
            "rule_id": "MOTOR_CURRENT_BIAS",
            "subsystem": "A",
            # 判据用**绝对残差** I − |τ|/kt，不用比值。
            # 零漂是恒定加法偏置（实测 +5A）：轻载 13A 时占 38%、重载 21A 时只占 24%，
            # 而正常负载本身就有 13~21A 的摆动——比值门槛要么轻载时把正常波动当故障，
            # 要么重载时漏掉。绝对残差对负载不敏感：正常时 I≈|τ|/kt 恒成立，偏置直接露出来。
            #
            # ⚠ 两处单位错误已修正（原式 `mean(current) - mean(tau_est)/0.25`）：
            #  1. kt 用错边：0.25 是**电机轴**常数，τ 是**关节**输出扭矩，须除以减速比
            #     6.33 后的 1.5825（见 `rig.py:59-78`）。用 0.25 时正常奔跑残差
            #     −44~−19，恒定偏置淹没在系统性偏差里，于是**正常工况也报**——
            #     实测 24 条故障矩阵里它出现 15 次，连 normal 都命中。
            #  2. tau_est 带符号、current 由 |τ| 派生：奔跑时 44% 的 τ 为负，
            #     `mean(tau_est)` 会正负抵消。必须用 `mean_abs`（先取绝对值再平均）。
            # 修正后实测（三姿态 × 3 seed）：正常窗口残差恒 0.00，F4 恒 5.00。
            "when": _win("window.mean(current) - window.mean_abs(tau_est) / 1.5825 >= 4.0"),
            "severity_hint": "warning",
            "message": ("电流与估计扭矩出现恒定错位（I − |τ|/kt ≥ 4A），疑似电流传感零漂"
                        "（项目假设：kt=1.5825 N·m/A 为关节侧值=0.25×减速比6.33，"
                        "4A 门槛无厂商出处）"),
            "field": "current",
            "provenance": "assumption",
        },

        # ==== A / 关节电机：力矩估算失效（F7）====
        # 与 F4 共用同一个残差 I − |τ|/kt，只是**符号相反、量级不同**：
        #   F4 电流读数被抬高 → 残差为正且恒 +5A（实测三姿态恒 5.00）；
        #   F7 tau_est 被压低 → |τ| 变小 → 残差同样为正，但随负载成比例：
        #      站立 0.36~0.40、趴卧 0.11~0.12、奔跑 2.48~2.71（tau 掉 45%）。
        # 所以这两条**不能只靠残差大小区分**——F7 在奔跑时残差 2.71，F4 恒 5.00。
        # 用 [1.5, 4.0) 这个区间接住 F7，4.0 以上交给 F4。
        # 与 F5（真实负载骤增）不会混：F5 的电流与扭矩同步上涨，残差恒为 0.00。
        #
        # ⚠ 已知边界：F7 的残差随负载成比例，**轻载姿态检不出**。
        #    实测站立 0.36~0.40、趴卧 0.11~0.12，都低于 1.5 门槛；
        #    只有奔跑（2.48~2.71）能过线。想要全姿态覆盖，得改成相对判据
        #    （残差 / mean(|τ|)），但站立的比值同样被噪声主导，暂无干净方案。
        {
            "rule_id": "MOTOR_TAU_EST_FAIL",
            "subsystem": "A",
            "when": _win("window.mean(current) - window.mean_abs(tau_est) / 1.5825 >= 1.5 and "
                         "window.mean(current) - window.mean_abs(tau_est) / 1.5825 < 4.0"),
            "severity_hint": "warning",
            "message": ("电流未变但力矩估计偏小（0.4~2.7A 残差，随负载成比例），"
                        "疑 FOC 的 kt 标定漂移（项目假设：1.5A 门槛；厂商未给判据）"),
            "field": "tau_est",
            "provenance": "assumption",
        },

        # ==== F / 整机负载：负载骤增（F5）====
        # 与 STALL 互斥用**堵转的物理定义**，不用 `max(current)` 硬切线。
        # 原先写的是 `max(current) < 14.5`，但实测 F5 峰值 14.69A、F1 堵转 14.98A
        # 只差 0.3A——一条 14.5A 的切线两边都会切错：真实的 F5 会被挡掉
        # （峰值自然越过 14.5），而接近饱和的负载又会被误当成堵转。
        # 现在改成"**不构成堵转**"这个语义条件，与 STALL 规则用的是同一套判据：
        #   是堵转 ⟺ 电流饱和占比 ≥50% **且** 关节静止占比 ≥50%
        # 所以"非堵转" = 两条里任意一条不成立。这样站立的 F5（电流才 1.71A，
        # 关节本来就不动）照样能过——它不饱和，不可能是堵转。
        #
        # ⚠ 阈值 50A→6.5A 同样是 kt 单位换算错误（50 ≈ 12.5N·m ÷ 0.25），不是调参。
        #   **这条规则在仿真器上属于弱判据，别指望它可靠**：实测奔跑正常
        #   mean(I)=5.50~6.02，F5=6.41~7.05，只差约 7%，任何固定电流门槛要么漏报
        #   要么误报。站立反而干净（正常 0.80~0.89 vs F5 1.70~1.72，正好 2 倍）。
        #   所以门槛按**站立可分的 1.4A** 取，代价是奔跑时判定不稳定——这是
        #   F5 注入强度（机身下压力 −60N）在奔跑步态里只占载荷一小部分导致的，
        #   属数据边界，不是规则能修的。
        #   同样地，`1.5 × 基线(τ)` 这条在奔跑上也不可靠：实测 F5 的
        #   mean|τ|/基线 站立 1.89、奔跑 1.16、趴卧 0.74——只有站立稳过线。
        #   **不要把门槛往上抬去"避开温和负载"**：抬到 2.0 以上，仿真器的 F5
        #   就一条也检不出（站立也只有 1.89）。
        #
        # ⚠ 与 TEMP_DEGRADE 的互斥，用"排除热退化画像"而不是抬阈值：
        #   `synth/generator.py` 的 F2 注入是「散热变差 + T_load += stall×0.25」，
        #   实测 τ/基线=2.88——已经越过 1.5× 门槛，于是 F2 同时命中 LOAD_SPIKE。
        #   想在 synth 上分开，门槛得抬到 3.0 以上（F2=2.88 / F5=5.55）；
        #   但 go2_sim 的 F5 站立只有 1.89，抬上去就全漏。**两个数据源对 F5 的
        #   建模强度不同，一个绝对阈值不可能同时满足两边。**
        #   能在两边同时成立的是「F2 的画像」：温度已进热退化区（≥40°C 且
        #   斜率 ≥0.25°C/s）而电流仍在中载（<7.9A，与 TEMP_DEGRADE 同一条边界）。
        #   把这幅画像排除掉，剩下的大电流+大扭矩才是单纯的外部负载。
        #   实测（synth）：F2 被排除 ✅、F5 meanI=11.18 保留 ✅；
        #   实测（go2_sim 三姿态）：F5 站立仍 1.00 命中、F2 不再误命中 LOAD_SPIKE ✅。
        # ⚠ 基线的口径必须与统计量一致：`mean_abs` 配 `baseline_abs`。
        #   原先写的是 `mean_abs(tau_est) >= 1.5 * baseline(tau_est)`——左边是
        #   |τ| 的均值，右边是**有符号** τ 的 EMA。步态摆动时 τ 正负对称振荡，
        #   有符号均值被正负抵消，于是比值里混进一个**与负载无关的恒定倍数**：
        #   go2_sim 奔跑实测基线 4.88、mean|τ| 9.14 → 比值恒 1.90，正常奔跑
        #   100% 误报 LOAD_SPIKE（240/240 帧）。
        #   趴卧更极端：基线是负数（−0.66），`mean|τ| >= 1.5×负数` 恒为真，
        #   但那一路被 sat/meanI 条款挡住了，问题一直没暴露。
        #   换成 `baseline_abs` 后两边同口径，倍数被抵消掉，剩下才是真实负载变化。
        {
            "rule_id": "MOTOR_LOAD_SPIKE",
            "subsystem": "F",
            "when": _win(
                "window.mean(current) >= 1.4 and "
                'window.count(current, ">=", 14.5) < 0.5 * window.samples() and '
                "window.mean_abs(tau_est) >= 1.5 * window.baseline_abs(tau_est) and "
                "not (window.mean(temperature) >= 40 and "
                "window.slope(temperature) >= 0.25 and "
                "window.mean(current) < 7.9)"),
            "severity_hint": "warning",
            "message": ("电流与估计扭矩同步大幅上升、未达堵转饱和，判定为外部负载骤增"
                        "（项目假设：1.4A / 1.5×基线；厂商未给负载阈值）"),
            "field": "tau_est",
            "provenance": "assumption",
        },

        # ==== A / 关节电机：编码器跳变 ====
        # max_step 的物理含义依赖采样率，所以先用 span 把采样率**锁到 ≥50Hz**：
        # 低于这个速率，"单步 3rad"可能是真实的高角速度而不是编码器故障。
        {
            "rule_id": "MOTOR_ENCODER_JUMP",
            "subsystem": "A",
            "when": _win("window.samples() / window.span() >= 50 and "
                         "window.max_step(q) >= 3.0"),
            "severity_hint": "critical",
            "message": ("≥50Hz 采样下关节角单步跳变 ≥3rad，超出关节物理角速度上限，"
                        "疑编码器故障（项目假设阈值；更强的判据需 q 与 q_raw 对比，"
                        "见'需采集侧补字段'清单）"),
            "field": "q",
            "provenance": "assumption",
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
            "多字段 + 窗口均值/斜率/计数/基线比较无法用单字段阈值表达；"
            "仍未到 WASM（无需自定义协议）"
        ),
    )


def main() -> int:
    ensure_utf8_console()
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
