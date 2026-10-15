import type {
  AlertEvent,
  CandidateEvent,
  InvestigationReport,
  InvestigationResult,
} from "@/lib/api/types";

export const candidate: CandidateEvent = {
  candidate_id: "cand_ab12cd34ef56",
  device_sn: "device-abc123",
  component_ref: "joint_motor.front_left_hip",
  signal: "temperature_rising_under_normalized_load",
  severity_hint: "critical",
  window_start: "2026-08-13T10:05:00Z",
  window_end: "2026-08-13T10:06:00Z",
  sample_count: 1,
  rule_matches: ["MOTOR_TEMP_PROTECT"],
  anomaly_score: 0.9,
  evidence: [
    {
      key: "temperature_c",
      from_value: 58,
      to_value: 96,
      baseline: null,
      unit: "°C",
      note: null,
    },
  ],
  evidence_refs: ["metricwin://cand_ab12cd34ef56"],
  detector_provenance: "op.temperature_protect:1",
};

export const candidatePage = {
  ok: true as const,
  count: 1,
  total: 1,
  limit: 20,
  offset: 0,
  has_more: false,
  items: [candidate],
};

export const alert: AlertEvent = {
  event_id: "alert_9f8e7d6c",
  idempotency_key: "idem_1",
  device_ref: "device-abc123",
  component_ref: "joint_motor.front_left_hip",
  event_type: "motor_over_temperature",
  state: "confirmed",
  severity: "critical",
  confidence: 0.9,
  detected_at: "2026-08-13T10:06:10Z",
  event_revision: 2,
  summary: "左前髋关节电机温度越过厂商 90°C 保护线。",
  recommended_actions: ["停机检查", "检查散热"],
  evidence_receipt: "evidence-receipt://abc",
  investigation_id: "inv_cand_ab12cd34ef56",
  policy_version: "policy-1",
  history: [
    { from: null, to: "detected", reason: "", at: 1_760_000_000 },
    { from: "detected", to: "confirmed", reason: "auto", at: 1_760_000_010 },
  ],
};

export const alertPage = {
  ok: true as const,
  count: 1,
  total: 1,
  limit: 50,
  offset: 0,
  has_more: false,
  items: [alert],
};

export const report: InvestigationReport = {
  investigation_id: "inv_cand_ab12cd34ef56",
  disposition: "needs_human_review",
  summary: "温度 96°C 超过 90°C 保护线，建议人工复核。",
  candidate_refs: ["cand_ab12cd34ef56"],
  event_type: "motor_over_temperature",
  severity: "critical",
  confidence: 0.72,
  hypotheses: [
    {
      cause: "散热不良导致过热",
      status: "supported",
      evidence_refs: ["metricwin://cand_ab12cd34ef56"],
      counter_evidence_refs: [],
    },
  ],
  counter_evidence_refs: [],
  missing_evidence: ["缺历史同部件温度基线"],
  risk_window: "2026-08-13T10:05:00Z/2026-08-13T10:06:00Z",
  recommended_actions: ["建议停机检查"],
  model_provenance: { provider: "deepseek", model: "deepseek-chat", call_count: 3, total_tokens: 1234 },
  correlation_group_id: null,
  linked_candidate_refs: [],
};

export const investigationResult: InvestigationResult = {
  ok: true,
  investigation_id: "inv_cand_ab12cd34ef56",
  candidate_id: "cand_ab12cd34ef56",
  report,
  verdict: "⚠️ 检测到 1 条严重异常",
  verdict_detail: "左前髋关节电机温度 96°C，超过厂商文档写明的 90°C 安全保护线。",
  trace: [
    {
      type: "tool_call",
      step: 1,
      name: "query_candidate_evidence",
      args: { device_sn: "device-abc123", component: "joint_motor.front_left_hip" },
      result: {
        device_sn: "device-abc123",
        component: "joint_motor.front_left_hip",
        metrics: { temperature_c: 96.0, current_a: 12.0 },
      },
    },
  ],
  usage: { call_count: 3, total_tokens: 1234, cost_usd: 0.0004, cost_estimated: false },
};
