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
    for name in ("mode", "temperature", "dq", "tau_est", "lost", "q", "q_raw"):
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
        # 也有尖峰），而 ≥90A 的占空比在堵转是 1.000、其余全部 0.000。
        {
            "rule_id": "MOTOR_STALL_SUSTAINED",
            "subsystem": "A",
            "when": _win("window.span() >= 5.0 and "
                         'window.count(current, ">=", 90) >= 0.5 * window.samples()'),
            "severity_hint": "critical",
            "message": ("电流在 ≥5s 窗口内有一半以上时间贴近堵转电流（项目假设：90A≈堵转扭矩"
                        "23.7N·m÷kt 0.25 的 95%；厂商文档未给电流阈值）"),
            "field": "current",
            "provenance": "assumption",
        },

        # ==== B / 热：热退化（F2）====
        # 与 F5 的区别是**负载量级**，不是"温升有无"——两者都会让电流上升（见报告）。
        {
            "rule_id": "MOTOR_TEMP_DEGRADE",
            "subsystem": "B",
            "when": _win("window.mean(temperature) >= 40 and "
                         "window.slope(temperature) >= 0.25 and "
                         "window.mean(current) < 50"),
            "severity_hint": "warning",
            "message": ("温度持续爬升且电流仍在中载区间（项目假设：40°C / 0.25°C·s⁻¹ / 50A；"
                        "厂商文档未给热退化判据）"),
            "field": "temperature",
            "provenance": "assumption",
        },

        # ==== E / 通信：丢包（F3，观测侧）====
        # 窗口只收有限数值，丢包样本不留占位 → 字段有效率下降就是丢包的可观测信号。
        {
            "rule_id": "COMM_PACKET_LOSS",
            "subsystem": "E",
            "when": _win("window.coverage(temperature) < 0.95"),
            "severity_hint": "warning",
            "message": ("窗口内该部件温度有效率低于 95%（项目假设阈值；厂商未给丢包容忍度）"),
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
            # 判据用**绝对残差** I − τ/kt，不用比值。
            # 零漂是恒定加法偏置（实测 +5A）：轻载 13A 时占 38%、重载 21A 时只占 24%，
            # 而正常负载本身就有 13~21A 的摆动——比值门槛要么轻载时把正常波动当故障，
            # 要么重载时漏掉。绝对残差对负载不敏感：正常时 I≈τ/kt 恒成立，偏置直接露出来。
            # 因线上没有转速，无法区分"传感器偏置"与"库仑摩擦/铁损"，
            # 故按物理不变量 I ≈ τ/kt 建模，只认电流与扭矩间的**恒定错位**。
            "when": _win("window.mean(current) - window.mean(tau_est) / 0.25 >= 4.0"),
            "severity_hint": "warning",
            "message": ("电流与估计扭矩出现恒定错位（I − τ/kt ≥ 4A），疑似电流传感零漂"
                        "（项目假设：kt=0.25 N·m/A 与 4A 门槛均无厂商出处）"),
            "field": "current",
            "provenance": "assumption",
        },

        # ==== F / 整机负载：负载骤增（F5）====
        # 用 max(current)<90 把"持续堵转"排除在负载骤增之外（堵转是另一条规则的事）。
        {
            "rule_id": "MOTOR_LOAD_SPIKE",
            "subsystem": "F",
            "when": _win("window.mean(current) >= 50 and window.max(current) < 90 and "
                         "window.mean(tau_est) >= 1.5 * window.baseline(tau_est)"),
            "severity_hint": "warning",
            "message": ("电流与估计扭矩同步大幅上升、未达堵转饱和，判定为外部负载骤增"
                        "（项目假设：50A / 1.5×基线；厂商未给负载阈值）"),
            "field": "tau_est",
            "provenance": "assumption",
        },

        # ==== A / 关节电机：编码器跳变（F6）====
        # 判据是 **`q` 与 `q_raw` 的偏离**，不是 max_step。
        #
        # 为什么换掉 max_step：`max_step(q)` 的含义**随采样率变化**——10Hz 下正常
        # 摆动一步就能超过 0.3rad，而 100Hz 下同样速度只有 0.03rad。旧判据因此
        # 必须先锁 `samples/span >= 50Hz`，而 go2_sim 按 publish_hz=10 投递，
        # 这条前置条件**恒不成立**（实测 F6 注入下 samples/span≈10），规则永不出手。
        # 更根本的是"单步跳多少 rad 算异常"本身要按关节和力矩另行标定。
        #
        # 为什么偏离更好：编码器故障的**定义性**特征是"报出来的角度与码盘原始值对不上"
        # （go2_sim/rig.py 的 F6 就是把 q 加 0.18 而 raw 不动）。这个量是**同一时刻
        # 两个通道的差**，与采样率无关，也不需要知道关节的物理角速度上限。
        #
        # 阈值 0.01 rad 的来处（全部为**项目假设**，厂商未给编码器偏差容限）：
        # 实测 go2_sim 60s、seed=7、60 点滑窗 @10Hz 的 abs(mean(q)-mean(q_raw))：
        #     正常      max 0.00038（q 无噪声，raw = q + N(0, 0.0015) 的均值≈0）
        #     F6 sev=0.1 min 0.01771（注入 0.018 rad，最小档）
        #     F6 sev=1.0     0.1797
        # 0.01 落在两者之间：距正常侧 26×，距最弱故障侧 1.8×。
        # **诚实边界**：0.0015 rad 的量化噪声是仿真器给的，真机码盘噪声量级未知；
        # 真机接入后必须用实采数据重定这个数，不能沿用。
        {
            "rule_id": "MOTOR_ENCODER_JUMP",
            "subsystem": "A",
            "when": _win("abs(window.mean(q) - window.mean(q_raw)) >= 0.01"),
            "severity_hint": "critical",
            "message": ("关节角 q 与编码器原始值 q_raw 的窗口均值偏离 ≥0.01rad，"
                        "判定为编码器通道故障而非机构真的偏了（项目假设阈值："
                        "仿真实测正常 ≤0.00038、最弱故障 ≥0.0177，仿真噪声量级 0.0015rad；"
                        "真机需用实采数据重定）"),
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
