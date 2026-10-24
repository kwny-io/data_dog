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
  /** **epoch 秒（float）**，不是 ISO 字符串。`/v1/telemetry` 刚接入的候选没有这个字段，
   *  `/v1/candidates` 回落时才带上（后端 `storage/models.py` 里 `created_at: Mapped[float]`）。
   *  要展示就 `new Date(created_at * 1000)`；按字符串处理（`.slice()` / `new Date(str)`）会拿到
   *  `Invalid Date`——而 TypeScript 又不会报错，因为这里曾经写的就是 `string`。 */
  created_at?: number;
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
  /** 请求是否被正常处理。**503/207 时也会返回 200 形状的对象，此处为 false**（见 error/detail）。 */
  ok: boolean;
  /**
   * 批内**有**事件被拒（等价于 `stats.rejected > 0`）。
   *
   * 后端是批处理接口、限流做在批内每个事件上，所以 **HTTP 200 + ok:true 不等于“全部落库”**。
   * 判断“真落库”请看 `persisted` 与 `stats.normalized`。语义详见 `docs/data_plane.md` §3.5（#102）。
   * 这个字段曾经在后端有、在这个 interface 里没有，于是本仓库自己的 TS 客户端**看不到它**，
   * 而文档却要求调用方看它——契约漂移已由 `tests/test_web_api_contract.py` 盯住。
   */
  partial: boolean;
  stats: IngestStats;
  /** 同步路径是 {telemetry, candidates}；write-behind 路径是 {mode:"write_behind", queued:true}，
   *  此时**不保证已落库**；失败路径（503/207）下不存在。 */
  persisted?: { telemetry: number; candidates: number } | { mode: "write_behind"; queued: boolean };
  candidates: CandidateEvent[];
  /** 仅失败路径存在：write_behind_unavailable / write_behind_queue_full / persist_failed。 */
  error?: string;
  detail?: string;
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

// ------------------------------------------------- resolved evidence refs

/** One entry of `GET /v1/candidates/{id}/refs`.
 *
 * `ref` is the parsed opaque reference; `payload` is whatever the scheme answered
 * (`rawlog` → the log body + digest + truncation flags, `metricwin` → the window's
 * metrics). On failure only `error`/`detail` are present — branch on `error`, never
 * parse `detail`. */
export interface ResolvedRef {
  ok: boolean;
  source?: string;
  ref?: { scheme: string; authority: string; path: string[]; raw?: string };
  payload?: Record<string, unknown>;
  error?: string;
  detail?: string;
}

/** `GET /v1/candidates/{id}/refs` — opaque refs resolved into readable evidence. */
export interface CandidateRefs {
  ok: true;
  candidate_id: string;
  results: ResolvedRef[];
  resolved: number;
  total: number;
}

// ------------------------------------------------------------- outbox

export interface OutboxRow {
  id?: number | string;
  event_id?: string;
  status?: string;
  attempts?: number;
  /** **epoch 秒（float）**，同 `CandidateEvent.created_at`：后端 `outbox.created_at` 是
   *  `Mapped[float]`，`repository.list_outbox()` 原样带出。展示要 `* 1000`。 */
  created_at?: number;
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
