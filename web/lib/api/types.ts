/**
 * Wire types for the data_dog HTTP API.
 *
 * Source of truth: the frontend spec §3.6 ("照抄字段" from the backend
 * dataclasses). Where the spec and the backend disagree, this file follows
 * the SPEC because it is the frontend's contract, and each such field carries
 * a NOTE saying whether the backend emits it today.
 *
 * The JSON Schemas in the backend's `schemas/*.json` are the *planned*
 * contract and deliberately differ (nested windows, `tenant_id`, an object
 * `detector_provenance`). The HTTP API emits the flat dataclass shape below.
 */

// ---------------------------------------------------------------- envelope

/** Finite error-code set from the backend `errors.py`. */
export type ApiErrorCode =
  | "denied"
  | "incompatible_binding"
  | "invalid_input"
  | "not_found"
  | "timeout"
  | "unavailable"
  | "execution_failed"
  // Extras the backend can emit that are not in the spec's table:
  | "contract_mismatch"
  | "ambiguous"
  | "version_conflict"
  | "rate_limited"
  | "persist_failed";

export interface ApiErrorEnvelope {
  ok: false;
  error: ApiErrorCode;
  detail?: unknown;
  reason?: string;
}

/** Every list endpoint uses this envelope. */
export interface Page<T> {
  ok: true;
  count: number;
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
  items: T[];
}

export interface PageParams {
  limit?: number;
  offset?: number;
}

// ------------------------------------------------------------- primitives

/** `evidence[]` entry on a candidate (backend `schemas/events.py`). */
export interface EvidenceSummary {
  key: string;
  from_value: number | string | null;
  to_value: number | string | null;
  baseline: number | string | null;
  unit: string | null;
  note: string | null;
}

/** A single ReAct trace step (AgentLoop `trace_cb`). */
export interface TraceStep {
  type: "tool_call";
  step: number;
  name: InvestigationToolName;
  args: Record<string, unknown>;
  result: Record<string, unknown>;
  /** 非空 = 这条工具返回里命中了提示注入特征（A3，core/injection.py）。
   *  后端已带（进 trace 与运行日志）；控制台的徽标渲染尚未做，见 docs/prompt_injection.md。 */
  injection_hits?: string[];
}

export type InvestigationToolName =
  | "query_candidate_evidence"
  | "query_vendor_knowledge"
  | "query_neighbor_events"
  | "query_recent_changes";

// ----------------------------------------------------------------- events

export type SeverityHint = "info" | "warning" | "critical";

/** Backend `schemas/events.py::CandidateEvent` — emitted flat today. */
export interface CandidateEvent {
  candidate_id: string;
  device_sn: string;
  component_ref: string;
  signal: string;
  severity_hint: SeverityHint;
  window_start: string;
  window_end: string;
  sample_count: number;
  rule_matches: string[];
  anomaly_score: number;
  evidence: EvidenceSummary[];
  evidence_refs: string[];
  detector_provenance: string; // "operator_id:version"
  /** Added by the tenant refactor; present on multi-tenant rows only. */
  tenant_id?: string;
  created_at?: string;
}

export type Disposition =
  | "confirmed"
  | "rejected"
  | "inconclusive"
  | "needs_human_review";

export type HypothesisStatus = "supported" | "unsupported" | "unverified";

export interface Hypothesis {
  cause: string;
  status: HypothesisStatus;
  evidence_refs: string[];
  counter_evidence_refs: string[];
}

export interface ModelProvenance {
  provider?: string;
  model?: string;
  call_count?: number;
  total_tokens?: number;
  [k: string]: unknown;
}

/** Backend `schemas/events.py::InvestigationReport`. */
export interface InvestigationReport {
  investigation_id: string;
  disposition: Disposition;
  summary: string;
  candidate_refs: string[];
  event_type: string | null;
  severity: string | null;
  confidence: number;
  hypotheses: Hypothesis[];
  counter_evidence_refs: string[];
  missing_evidence: string[];
  risk_window: string | null;
  recommended_actions: string[];
  model_provenance: ModelProvenance;
  correlation_group_id: string | null;
  linked_candidate_refs: string[];
}

// ----------------------------------------------------------------- alerts

export type AlertState =
  | "detected"
  | "confirmed"
  | "escalated"
  | "recovering"
  | "resolved"
  | "suppressed"
  | "rejected"
  | "closed";

export type AlertSeverity = "warning" | "critical";

export interface AlertHistoryEntry {
  from: AlertState | null;
  to: AlertState;
  reason?: string;
  at?: number | string;
}

/** Backend `alerting/lifecycle.py::AlertEvent`. */
export interface AlertEvent {
  event_id: string;
  idempotency_key: string;
  device_ref: string;
  component_ref: string;
  event_type: string;
  state: AlertState;
  severity: AlertSeverity;
  confidence: number;
  detected_at: string;
  event_revision: number;
  summary: string;
  recommended_actions: string[];
  evidence_receipt: string;
  investigation_id: string;
  policy_version: string;
  history: AlertHistoryEntry[];
}

// -------------------------------------------------------------- telemetry

export interface IngestStats {
  ingested: number;
  resolved: number;
  rejected: number;
  rejected_by_reason: Record<string, number>;
  normalized: number;
  candidates: number;
}

export interface IngestResult {
  ok: true;
  stats: IngestStats;
  persisted: { telemetry: number; candidates: number };
  candidates: CandidateEvent[];
}

// ------------------------------------------------- investigation (spec §3.5)

export interface InvestigationUsage {
  call_count: number;
  total_tokens: number;
  cost_usd: number;
  cost_estimated: boolean;
}

/**
 * Response of `POST /v1/investigations`.
 *
 * NOTE: this endpoint does not exist in the backend yet (spec stage D). The
 * shape is taken from the spec so the UI is ready when it lands; until then
 * the dev msw handler serves it.
 */
export interface InvestigationResult {
  ok: true;
  investigation_id: string;
  candidate_id: string;
  report: InvestigationReport;
  verdict: string;
  verdict_detail: string;
  trace: TraceStep[];
  usage: InvestigationUsage;
}

/** `GET /v1/investigations/jobs/{id}` — backend `runtime._job_public`.
 *
 * Async mode: `POST /v1/investigations` returns `202 {job_id}` and the LLM work
 * happens in a worker pool. Poll until terminal, then take `.../result` (same
 * shape as the synchronous response: report + verdict + trace + usage).
 */
export interface InvestigationJob {
  job_id: string;
  candidate_id: string;
  status: "pending" | "running" | "succeeded" | "failed" | "cancelled";
  report_id: string | null;
  error_code: string | null;
  [k: string]: unknown;
}

/** `GET /v1/candidates/{id}/evidence` (spec §3.5; backend stage D). */
export interface CandidateEvidence {
  ok: true;
  candidate_id: string;
  device_sn: string;
  component_ref: string;
  window: { start: string; end: string };
  metrics: Record<string, number>;
  reference_window_metrics: Record<string, number>;
  evidence: EvidenceSummary[];
  vendor_knowledge: Array<Record<string, unknown>>;
}

// ------------------------------------------------------------- outbox

export interface OutboxRow {
  id?: number | string;
  event_id?: string;
  status?: string;
  attempts?: number;
  created_at?: string;
  [k: string]: unknown;
}

// ----------------------------------------------------- probes & summary(§3.7)

export interface Healthz {
  status: string;
  service: string;
  version: string;
  uptime_s: number;
}

export interface Readyz {
  status: "ready" | "not_ready";
  checks: Record<string, unknown>;
}

/** `GET /v1/summary` (spec §3.7; optional backend endpoint, stage D). */
export interface Summary {
  ok: true;
  uptime_s: number;
  candidates_total: number;
  alerts_by_state: Partial<Record<AlertState, number>>;
  outbox_pending: number;
  resolver_ready: boolean;
  auth_enabled: boolean;
}
