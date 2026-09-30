"""E2E 编排：厂商文档 →[P1]→ 算子 →[P2]→ 候选 →[Agent]→ 报告 →[P3]→ isSaaS 事件。

一条命令跑通整链，产出可核对的中间产物（算子数、候选数、报告、事件、Receipt）。
全程**离线、确定性**（Agent 用确定性 Provider，不依赖 LLM），保证可复现。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..alerting.saas import SaaSTestAdapter
from ..alerting.service import AlertReportingService
from ..data_plane.pipeline import DataPlane, build_demo_resolver
from ..mujoco_sim.mujoco_eval import MujocoDeterministicProvider
from ..operator_factory.knowledge import build_knowledge_package
from ..operator_factory.p1_demo import build_motor_operator
from ..paths import vendor_docs_dir
from ..schemas.operator_config import DetectorConfig, FieldSpec, ThresholdRule
from ..tools.investigation import InvestigationTools
from .bridge import CandidateEvidenceBridge


@dataclass
class E2EResult:
    operator_id: str = ""
    operator_fields: int = 0
    operator_rules: int = 0
    ingested: int = 0
    resolved: int = 0
    rejected: int = 0
    candidates: list[Any] = field(default_factory=list)
    reports: list[Any] = field(default_factory=list)
    decisions: list[Any] = field(default_factory=list)
    alerts_delivered: int = 0
    feedback: list[dict[str, Any]] = field(default_factory=list)
    # 降级段（G4）：仅 `with_degradation=True` 时填充；默认空 → summary 不含该键
    degradation: list[dict[str, Any]] = field(default_factory=list)
    degradation_queued: int = 0
    degradation_drained: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "operator": {"id": self.operator_id, "fields": self.operator_fields,
                         "rules": self.operator_rules},
            "data_plane": {"ingested": self.ingested, "resolved": self.resolved,
                           "rejected": self.rejected, "candidates": len(self.candidates)},
            "agent": {"reports": len(self.reports),
                      "dispositions": [r.disposition for r in self.reports]},
            "alerting": {"decisions": [d.report for d in self.decisions],
                         "delivered": self.alerts_delivered},
        }
        # 仅在开启降级段时加入——关闭时输出与既有回放**逐字节一致**（零回归）。
        if self.degradation:
            out["degradation"] = {
                "paths": [d["path"] for d in self.degradation],
                "queued": self.degradation_queued,
                "drained": self.degradation_drained,
            }
        return out


def _detector_config() -> DetectorConfig:
    pkg = build_knowledge_package(vendor_docs_dir() / "unitree" / "cleaned")
    op = build_motor_operator(pkg)
    return DetectorConfig(
        operator_id=op.operator_id, operator_type="parser+detector",
        component="joint_motor.front_left_hip",
        input_schema="raw-log-envelope-v1", output_schema="normalized-telemetry-v1",
        fields=[FieldSpec(**f) for f in op.fields],
        rules=[ThresholdRule(r.rule_id, r.field, r.operator, r.value,
                             r.severity_hint, r.message) for r in op.rules],
    )


class _CandidateAwareProvider(MujocoDeterministicProvider):
    """按**候选实际的设备/部件**查询的确定性 Provider。

    修掉一个真实的不一致：上游 `MujocoDeterministicProvider` 写死去查
    `front_left_knee` + `device-synth`，而 P2 产生的候选是 `front_left_hip` +
    `device-go2-0001`——导致工具返回 not_found、裁决退化。
    这里从候选推导查询参数，保证整链数据对齐（设备/部件一致）。
    """

    def __init__(self, device_sn: str, component: str):
        super().__init__()
        self._device_sn = device_sn
        self._component = component
        self.n = 0

    def chat(self, messages, tools=None, model=None, temperature=0.2, max_tokens=2048):
        self.n += 1
        if self.n == 1:
            return {
                "kind": "tool_calls",
                "tool_calls": [{
                    "id": "s1", "name": "query_candidate_evidence",
                    "arguments": json.dumps({
                        "device_sn": self._device_sn,
                        "component": self._component,
                        "window_start": "09:55", "window_end": "10:00",
                    }),
                    "content": "",
                }],
                "__raw_tool_calls": [],
            }
        tel = self._find_telemetry(messages)
        return {
            "kind": "text",
            "content": json.dumps(self._decide(tel), ensure_ascii=False),
        }


def _raw(event_id: str, **kw):
    base = dict(event_id=event_id, occurred_at="2026-09-13T10:00:00Z",
                received_at="2026-09-13T10:00:00.1Z", sequence=1,
                vendor="unitree", model="go2", firmware="3.2.1",
                protocol_version="motor-log-v2",
                component_hint="joint_motor.front_left_hip")
    base.update(kw)
    return base


def _telemetry_for(metrics: dict[str, Any], peak: float, current: float) -> dict[str, Any]:
    """把 P2 规范化后的 metrics 转成 Agent 证据源认识的窗口查询结果。"""
    return {
        "device_sn": "device-go2-0001",
        "component": "joint_motor.front_left_hip",
        "window": "09:55-10:00",
        "metrics": {"temperature_c": peak, "current_a": current},
        "peak_temperature_c": peak,
        "reference_window_metrics": {"temperature_c": 40.0, "current_a": 6.0},
    }


def _run_degradation_segment(result: E2EResult) -> None:
    """降级段（G4，旁路）：把 P2 候选喂给 `DegradationPolicy` 观察三条路径。

    场景（对齐 §9.4）：
      1. Agent 不可用 → 普通候选排队、硬规则候选直报；
      2. 队列满 → 溢出候选退化为直报（不无限堆积）；
      3. Agent 恢复 → drain 取回排队候选。

    只写 `result.degradation*`，不改主链（reports/decisions）；关闭时这些字段为空。
    """
    from ..agent.degradation import DegradationPolicy

    cands = result.candidates

    # 场景 1+2：Agent 不可用，队列容量故意设小以观察溢出。
    capacity = max(1, len(cands) - 1)
    down = DegradationPolicy(agent_available=False, queue_capacity=capacity)
    down_decisions = down.decide_batch(cands)
    result.degradation = [d.to_dict() for d in down_decisions]
    result.degradation_queued = len(down.queued)

    # 场景 3：Agent 恢复 → drain 取回（不重复调查，仅验证队列可续）。
    drained = down.drain()
    down.agent_available = True
    result.degradation_drained = drained


def run_e2e(*, auth: dict[str, Any] | None = None,
            with_degradation: bool = False) -> E2EResult:
    auth = auth or {"tenant_id": "tenant-real", "device_sn": "device-go2-0001"}
    result = E2EResult()

    # ---- P1：厂商文档 → 算子 ----
    cfg = _detector_config()
    result.operator_id = cfg.operator_id
    result.operator_fields = len(cfg.fields)
    result.operator_rules = len(cfg.rules)

    # ---- P2：原始日志 → 候选 ----
    dp = DataPlane(build_demo_resolver(cfg))
    batch = [
        # 正常温度
        _raw("e1", payload_inline=json.dumps({"temperature": 42, "current": 6.0, "mode": 0})),
        # 过温（应命中 90°C 保护）
        _raw("e2", payload_inline=json.dumps({"temperature": 95, "current": 12.0, "mode": 0})),
        _raw("e3", payload_inline=json.dumps({"temperature": 98, "current": 13.0, "mode": 0})),
        # 固件不兼容（失败关闭）
        _raw("e4", firmware="9.9.9", payload_inline="{}"),
    ]
    dp_res = dp.process_batch(batch, auth)
    result.ingested = dp_res.stats.ingested
    result.resolved = dp_res.stats.resolved
    result.rejected = dp_res.stats.rejected
    result.candidates = dp_res.candidates

    if not result.candidates:
        return result

    # ---- 降级段（G4，默认关）：候选在 Agent 不可用/可用/队列满下的处置 ----
    # 旁路观测，不改主链；开启时才填充 result.degradation → 关闭时输出零回归。
    if with_degradation:
        _run_degradation_segment(result)

    # ---- 桥接证据源：把候选部件对应的高温遥测喂给 Agent ----
    # 取最后一个规范化事件里该部件的温度作为峰值
    comp = "joint_motor.front_left_hip"
    peak, cur = 98.0, 13.0
    for n in reversed(dp_res.normalized):
        if n.component_ref == comp:
            peak = float(n.metrics.get("temperature", peak))
            cur = float(n.metrics.get("current", cur))
            break
    bridge = CandidateEvidenceBridge({comp: _telemetry_for({}, peak, cur)},
                                     device_sn=auth["device_sn"])

    # ---- Agent：受控调查（确定性 Provider，不调 LLM）----
    from ..agent.loop import AgentLoop

    # ---- P3：策略门 → 正式事件 → SaaS ----
    svc = AlertReportingService(adapter=SaaSTestAdapter())

    for cand in result.candidates:
        # 每候选一个 provider：按该候选的设备/部件查证据（整链数据对齐）
        tools = InvestigationTools(bridge)
        agent = AgentLoop(
            _CandidateAwareProvider(cand.device_sn, cand.component_ref), tools)
        report = agent.investigate(cand)
        result.reports.append(report)
        # 组装 P3 信号：证据引用 + 候选严重度
        signals = {
            "evidence_refs": list(cand.evidence_refs) or ["metricwin://candidate"],
            "candidate_severities": [cand.severity_hint],
        }
        # 厂商严重码：候选命中 MOTOR_TEMP_PROTECT（90°C 保护）即视为厂商严重规则
        if "MOTOR_TEMP_PROTECT" in (cand.rule_matches or []):
            signals["vendor_severe_codes"] = ["OVERHEAT"]
        outcome = svc.process(
            {
                "investigation_id": report.investigation_id,
                "disposition": report.disposition,
                "confidence": report.confidence,
                "severity": report.severity or cand.severity_hint,
                "summary": report.summary,
                "recommended_actions": report.recommended_actions,
                "event_type": "joint_motor_thermal_degradation",
            },
            signals,
            device_ref="device-opaque-001", component_ref=cand.component_ref,
        )
        result.decisions.append(outcome.decision)
        if outcome.delivered and outcome.event is not None:
            result.alerts_delivered += 1
            # 人工反馈回填示例
            kind = "repaired" if report.disposition == "confirmed" else "false_positive"
            result.feedback.append(
                svc.adapter.submit_feedback(outcome.event.event_id, kind,
                                            note="E2E 回放反馈"))
    return result
