"""演示场景：用**真实 P2 数据面**从原始日志产出候选（供 UI / 回归测试复用）。

为什么要这个模块
----------------
早先 UI 的候选是**手写的 TEST_CASES**（`rule_matches=["TEST"]`、`anomaly_score=0.5`
这种占位值），看起来像"数据面产出的候选"，其实是剧本。这会造成两个问题：

1. 讲不清来源——被问"这些候选怎么来的"答不上；
2. **候选与证据可能不一致**——手写候选说 78.2°C，证据库里是另一份数字。

本模块把候选交给 `DataPlane` 真跑：原始日志 → 网关 → 兼容绑定 → 解析 → 归一化
→ 检测 → 候选合并。**候选、以及 Agent 随后查到的证据，都来自同一批原始日志**
（单一事实源），从根上消除不一致。

约定
----
- 设备固定 `device-abc123`：与 `context.default_context()` 的可信设备一致，
  这样 Agent 工具的设备注入校验能通过（见 tools/investigation.py 的注释）。
- 部件必须落在 `evidence.store.ALLOWED_COMPONENTS` 白名单内。
- 温度/电流在厂商 IDL 里声明为整数（temperature=uint8, current=int32），
  所以这里用整数——小数会被 Parser 按协议类型截断（这是正确行为，不是丢失精度）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..schemas.events import CandidateEvent
from .p2_demo import _detector_config
from .pipeline import DataPlane, DataPlaneResult, build_demo_resolver

# 与默认可信上下文一致的设备号（不可随意改，否则工具层设备校验会拒绝）
DEMO_DEVICE_SN = "device-abc123"
DEMO_TENANT_ID = "tenant-demo"

# P2 归一化后的字段名（来自 IDL：temperature/current/mode）
# → Agent 证据侧使用的标准名（对齐架构 §12.2：temperature_c / current_a）
_STANDARD_NAME = {
    "temperature": "temperature_c",
    "current": "current_a",
    "mode": "mode",
}

# 部件英文标识 → 中文显示名（只用于界面，不参与计算）
_COMPONENT_CN = {
    "joint_motor.front_left_hip": "左前髋关节电机",
    "joint_motor.front_right_hip": "右前髋关节电机",
    "joint_motor.front_left_knee": "左前膝关节电机",
    "joint_motor.front_right_knee": "右前膝关节电机",
    "battery.main": "主电池",
    "controller.main": "主控",
}


def component_cn(component_ref: str) -> str:
    """部件显示名：有中文就用中文，没有就原样返回。"""
    return _COMPONENT_CN.get(component_ref, component_ref)


@dataclass(frozen=True)
class Scenario:
    """一个演示场景：标签 + 关键词 + 原始日志。

    `keywords` 供演示台做"输入问题 → 关键字检索"用（不参与计算）。
    `note` 描述这个场景想说明什么；`expect_*` 仅供展示与回归测试，不参与运行。
    """
    key: str
    label: str
    component: str
    note: str
    events: list[dict[str, Any]]
    keywords: list[str] = field(default_factory=list)
    baseline_events: list[dict[str, Any]] = field(default_factory=list)
    expect_candidate: bool = True
    expect_severity: str = ""


def match_scenarios(query: str,
                    scenarios: list[Scenario] | None = None) -> list[Scenario]:
    """关键字检索：把用户描述的问题匹配到场景（空查询返回全部）。

    匹配 label + keywords + component，大小写不敏感，任一关键词命中即算匹配。
    """
    pool = scenarios if scenarios is not None else SCENARIOS
    q = (query or "").strip().lower()
    if not q:
        return list(pool)
    tokens = [t for t in q.replace("，", " ").replace(",", " ").split() if t]
    out = []
    for s in pool:
        hay = " ".join([s.label, s.component, s.note, *s.keywords]).lower()
        if any(t in hay for t in tokens):
            out.append(s)
    return out


@dataclass
class ScenarioRun:
    """一个场景跑完的结果。"""
    scenario: Scenario
    candidates: list[CandidateEvent]
    telemetry_by_component: dict[str, dict[str, Any]]
    result: DataPlaneResult | None = None

    @property
    def short_name(self) -> str:
        """给界面用的短名（取 label 破折号前那半段）。"""
        return self.scenario.label.split("——")[0].strip()

    @property
    def verdict(self) -> str:
        """一句话结论，给不想看细节的人看。"""
        if self.candidates:
            c = self.candidates[0]
            sev = {"critical": "严重", "warning": "警告"}.get(c.severity_hint,
                                                             c.severity_hint)
            return f"⚠️ 检测到 1 条{sev}异常"
        st_ = self.stats
        if st_.get("rejected"):
            return "⛔ 日志被拒绝（失败关闭）"
        return "✅ 没有检测到异常"

    @property
    def verdict_detail(self) -> str:
        """结论的一句人话解释。"""
        if self.candidates:
            c = self.candidates[0]
            name = component_cn(c.component_ref)
            temp = None
            for ev in self.telemetry_by_component.values():
                t = ev.get("metrics", {}).get("temperature_c")
                if t is not None:
                    temp = t
                    break
            if "MOTOR_TEMP_PROTECT" in c.rule_matches and temp is not None:
                return (f"{name}温度 {temp:.0f}°C，超过厂商文档写明的 90°C "
                        "安全保护线。")
            return f"{name}命中了检测规则：{', '.join(c.rule_matches)}。"

        if (self.stats.get("rejected_by_reason") or {}):
            return "设备信息与已知兼容范围对不上，数据面拒绝处理而不是猜测。"

        has_metrics = any(ev.get("metrics") for ev in self.telemetry_by_component.values())
        if not has_metrics:
            return ("这条日志的字段和当前检测算子对不上（本算子只覆盖关节电机），"
                    "解析不出可用指标，所以没有可判定的内容。")
        return "各项读数都在厂商文档给出的规则范围内，没有触发任何检测规则。"

    @property
    def stats(self) -> dict[str, Any]:
        if self.result is None:
            return {}
        st = self.result.stats
        return {
            "ingested": st.ingested,
            "resolved": st.resolved,
            "rejected": st.rejected,
            "rejected_by_reason": dict(st.rejected_by_reason),
            "normalized": len(self.result.normalized),
            "candidates": len(self.result.candidates),
        }


def _raw(event_id: str, *, occurred_at: str, temperature: int, current: int,
         component: str, firmware: str = "3.2.1", mode: int = 0,
         payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造一条原始日志（RawLogEnvelope 的输入形态）。

    `payload` 可覆盖默认的电机字段载荷——用于演示"字段与算子不匹配"的情形。
    """
    body = payload if payload is not None else {
        "temperature": temperature, "current": current, "mode": mode,
    }
    return {
        "event_id": event_id,
        "occurred_at": occurred_at,
        "received_at": occurred_at,
        "sequence": 1,
        "vendor": "unitree",
        "model": "go2",
        "firmware": firmware,
        "protocol_version": "motor-log-v2",
        "component_hint": component,
        "payload_inline": json.dumps(body),
    }


def _build_scenarios() -> list[Scenario]:
    hip = "joint_motor.front_left_hip"
    knee = "joint_motor.front_left_knee"
    rhip = "joint_motor.front_right_hip"
    ctrl = "controller.main"
    return [
        Scenario(
            key="overheat-with-change",
            label="左髋电机 96°C —— 过温，但近期有固件变更",
            component=hip,
            note=("越过厂商 90°C 保护线（critical 候选）。但该部件 8/12 有固件变更"
                  "（改过冷却参数）——证据里存在**两种都成立**的解释：固件效应 or "
                  "真实退化。离线确定性模式据此给 needs_human_review；"
                  "真实模型可能仍判 confirmed（判断空间，非对错）。"),
            keywords=["温度", "过温", "过热", "热", "电机", "左髋", "固件", "烧"],
            baseline_events=[_raw("b1", occurred_at="2026-08-13T10:00:00Z",
                                  temperature=58, current=6, component=hip)],
            events=[_raw("e1", occurred_at="2026-08-13T10:05:00Z",
                         temperature=96, current=12, component=hip)],
            expect_severity="critical",
        ),
        Scenario(
            key="overheat-no-change",
            label="左膝电机 95°C —— 过温，无变更干扰",
            component=knee,
            note=("同样越过保护线（critical 候选），但该部件**无近期变更记录**、"
                  "证据干净，只有一种解释 → 预期 confirmed。"),
            keywords=["温度", "过温", "过热", "热", "电机", "左膝", "膝盖"],
            baseline_events=[_raw("b2", occurred_at="2026-08-13T10:00:00Z",
                                  temperature=54, current=6, component=knee)],
            events=[_raw("e2", occurred_at="2026-08-13T10:05:00Z",
                         temperature=95, current=13, component=knee)],
            expect_severity="critical",
        ),
        Scenario(
            key="normal-no-alarm",
            label="右髋电机 61°C —— 正常数据，不产出候选（不误报）",
            component=rhip,
            note="温度远低于 90°C 保护线，无规则命中 → 数据面不产出候选。",
            keywords=["温度", "正常", "电机", "右髋", "误报", "没事", "排除"],
            events=[_raw("e3", occurred_at="2026-08-13T10:05:00Z",
                         temperature=61, current=6, component=rhip)],
            expect_candidate=False,
        ),
        Scenario(
            key="borderline-no-candidate",
            label="左膝电机 89°C —— 贴近阈值但未越线，不产出候选",
            component=knee,
            note="差 1°C 到保护线，规则不命中 → 候选 0 条（阈值是硬边界，不猜）。",
            keywords=["温度", "临界", "接近", "阈值", "电机", "左膝", "89"],
            baseline_events=[_raw("b3", occurred_at="2026-08-13T10:00:00Z",
                                  temperature=55, current=6, component=knee)],
            events=[_raw("e4", occurred_at="2026-08-13T10:05:00Z",
                         temperature=89, current=8, component=knee)],
            expect_candidate=False,
        ),
        Scenario(
            key="current-no-rule",
            label="左髋电流 13A 偏高（温度正常）—— 厂商无电流阈值，不报",
            component=hip,
            note=("电流明显高于基线 6A，但厂商文档里**没有**权威的电流告警阈值，"
                  "所以算子**不编规则** → 候选 0 条。这条演示「不编数」的代价："
                  "宁可漏报，也不拿假设当厂商事实。"),
            keywords=["电流", "负载", "偏高", "电机", "左髋", "规则", "为什么不报"],
            baseline_events=[_raw("b4", occurred_at="2026-08-13T10:00:00Z",
                                  temperature=52, current=6, component=hip)],
            events=[_raw("e5", occurred_at="2026-08-13T10:05:00Z",
                         temperature=60, current=13, component=hip)],
            expect_candidate=False,
        ),
        Scenario(
            key="controller-cpu",
            label="主控 CPU 52°C —— 本算子只覆盖关节电机（解析缺字段）",
            component=ctrl,
            note=("日志来自主控，字段是 cpu_temp_c / load，与关节电机不同；"
                  "当前 Parser 只声明了电机字段 → **解析不出指标**、检测无命中 "
                  "→ 候选 0 条。这是**能力边界**，不是故障：接主控需要另一套 "
                  "Parser/算子（尚未实现）。"),
            keywords=["主控", "控制器", "cpu", "温度", "算力", "边界", "不支持"],
            events=[_raw("e6", occurred_at="2026-08-13T10:05:00Z",
                         temperature=0, current=0, component=ctrl,
                         payload={"cpu_temp_c": 52.0, "load": 0.4})],
            expect_candidate=False,
        ),
        Scenario(
            key="fail-closed-firmware",
            label="固件版本不匹配 —— 失败关闭（不产出候选）",
            component=hip,
            note="固件 9.9.9 不在兼容范围；数据面拒绝而不是猜 → 候选 0 条。",
            keywords=["固件", "版本", "不匹配", "失败关闭", "兼容", "协议"],
            events=[_raw("e7", occurred_at="2026-08-13T10:05:00Z",
                         temperature=96, current=12, component=hip, firmware="9.9.9")],
            expect_candidate=False,
        ),
    ]


SCENARIOS: list[Scenario] = _build_scenarios()


def _telemetry_entry(component: str, metrics: dict[str, Any],
                     baseline: dict[str, Any] | None) -> dict[str, Any]:
    """把 P2 归一化 metrics 转成 Agent 证据源认识的窗口查询结果。"""
    std = {_STANDARD_NAME.get(k, k): float(v) for k, v in metrics.items()}
    entry: dict[str, Any] = {
        "device_sn": DEMO_DEVICE_SN,
        "component": component,
        "metrics": std,
    }
    if baseline:
        entry["reference_window_metrics"] = {
            _STANDARD_NAME.get(k, k): float(v) for k, v in baseline.items()}
    return entry


def run_scenario(scenario: Scenario, *,
                 device_sn: str = DEMO_DEVICE_SN,
                 tenant_id: str = DEMO_TENANT_ID) -> ScenarioRun:
    """在**独立的** DataPlane 上跑一个场景。

    每个场景用新实例：`CandidateBuilder` 是无时间淘汰的累积器，复用实例会把
    不同场景的命中错误合并（服务端同样按请求隔离，见 docs/delivery.md）。
    """
    resolver = build_demo_resolver(_detector_config())
    auth = {"tenant_id": tenant_id, "device_sn": device_sn}

    # 基线窗只用来取参照值，**不参与候选**：因此用独立的 DataPlane 跑，
    # 否则它的命中会累积进同一个 CandidateBuilder，基线也被当成异常。
    baseline_metrics: dict[str, dict[str, Any]] = {}
    if scenario.baseline_events:
        base_plane = DataPlane(resolver)
        base_res = base_plane.process_batch(scenario.baseline_events, auth)
        for ev in base_res.normalized:
            ref = f"{ev.component_type}.{ev.component_instance}"
            baseline_metrics[ref] = dict(ev.metrics)

    plane = DataPlane(resolver)
    result = plane.process_batch(scenario.events, auth)

    telemetry: dict[str, dict[str, Any]] = {}
    for ev in result.normalized:
        ref = f"{ev.component_type}.{ev.component_instance}"
        telemetry[ref] = _telemetry_entry(ref, ev.metrics, baseline_metrics.get(ref))

    return ScenarioRun(scenario=scenario, candidates=list(result.candidates),
                       telemetry_by_component=telemetry, result=result)


def run_all(scenarios: list[Scenario] | None = None) -> list[ScenarioRun]:
    """跑全部场景（顺序确定，便于 UI 与回归测试）。"""
    return [run_scenario(s) for s in (scenarios if scenarios is not None else SCENARIOS)]
