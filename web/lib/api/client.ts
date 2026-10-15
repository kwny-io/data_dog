/**
 * Single HTTP client for the data_dog `/v1/*` API.
 *
 * This module is the ONLY place that talks to the network. It never imports
 * anything from the Python backend — the whole point of the rewrite is that
 * the browser reaches the service over HTTP (spec §0).
 *
 * Responsibilities:
 *  - inject the auth/tenant headers on every request (spec §2);
 *  - classify `{ok:false}` responses into a typed ApiError carrying the
 *    finite `error` code — callers branch on the code, never parse `detail`;
 *  - support a long-timeout, abortable request for the investigative POST,
 *    which runs a synchronous LLM call and can take 10–60s (spec §3.5).
 */

import type {
  ApiErrorCode,
  ApiErrorEnvelope,
  InvestigationJob,
  InvestigationResult,
  Page,
  PageParams,
} from "./types";

// ------------------------------------------------------------------ config

/** Base path of the console's **server-side proxy** (`app/api/dd/[...path]`).
 *
 * Default `/api/dd` → same-origin, and the proxy attaches the API key/tenant on
 * the server. Tests pin an absolute URL because Node's `fetch` requires one.
 *
 * ⚠ **Never** put a credential here. `NEXT_PUBLIC_*` is inlined into the browser
 * bundle, which is exactly the leak this proxy exists to remove; the upstream
 * service address and the key live in server-only `DD_WEB_*` variables. */
export const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/dd").replace(
  /\/$/,
  "",
);

/** Default per-request timeout for ordinary reads/writes. */
const DEFAULT_TIMEOUT_MS = 15_000;

/** The investigative POST is synchronous and slow — allow well past the
 *  backend's DD_INVESTIGATE_TIMEOUT_S (default 90s). */
export const INVESTIGATE_TIMEOUT_MS = Number(
  process.env.NEXT_PUBLIC_INVESTIGATE_TIMEOUT_MS ?? 110_000,
);

/** Poll interval while an **async** investigation runs (`DD_INVESTIGATE_ASYNC=1`).
 *  One second: the backend job is a 10–60s LLM run, so faster polling only adds
 *  load; tests override it per call (`pollMs`). */
export const INVESTIGATE_POLL_MS = Number(
  process.env.NEXT_PUBLIC_INVESTIGATE_POLL_MS ?? 1000,
);

// ------------------------------------------------------------------ errors

/** HTTP status the client should show for a given code, when the server did
 *  not supply one. Kept in sync with the backend `errors.py` map. */
const DEFAULT_STATUS: Record<ApiErrorCode, number> = {
  denied: 403,
  incompatible_binding: 422,
  invalid_input: 422,
  not_found: 404,
  timeout: 504,
  unavailable: 503,
  execution_failed: 500,
  contract_mismatch: 422,
  ambiguous: 409,
  version_conflict: 409,
  rate_limited: 429,
  persist_failed: 207,
};

export class ApiError extends Error {
  readonly code: ApiErrorCode;
  readonly status: number;
  readonly detail?: unknown;
  readonly reason?: string;
  readonly requestId?: string;

  constructor(init: {
    code: ApiErrorCode;
    status: number;
    detail?: unknown;
    reason?: string;
    requestId?: string;
  }) {
    super(`[${init.status}] ${init.code}${init.reason ? `: ${init.reason}` : ""}`);
    this.name = "ApiError";
    this.code = init.code;
    this.status = init.status;
    this.detail = init.detail;
    this.reason = init.reason;
    this.requestId = init.requestId;
  }

  /** Cross-tenant reads return 404 by design (spec §3.0): the UI should say
   *  "not found", never "no permission". */
  get isNotFound(): boolean {
    return this.status === 404 || this.code === "not_found";
  }
}

/** Thrown when the request never reached the server (offline, DNS, CORS). */
export class NetworkError extends Error {
  override readonly cause?: unknown;
  constructor(message: string, cause?: unknown) {
    super(message);
    this.name = "NetworkError";
    this.cause = cause;
  }
}

/** Thrown when the client-side timeout fired, or the caller aborted. */
export class TimeoutError extends Error {
  constructor(message = "请求超时") {
    super(message);
    this.name = "TimeoutError";
  }
}

// ------------------------------------------------------------- header set

export interface RequestContext {
  /** Telemetry only. The backend identifies the device from this header; it is
   *  never sent on reads, and never placed in the body (§2, identity in
   *  headers only). */
  deviceSn?: string;
}

function buildHeaders(
  ctx: RequestContext,
  extra?: HeadersInit,
  hasBody = false,
): Headers {
  const h = new Headers(extra);
  // ⚠ No `X-API-Key` and no `X-Tenant-Id` here on purpose. Both are attached by
  // the server-side proxy (`app/api/dd`) from server-only `DD_WEB_*` variables;
  // anything the browser carries is readable in devtools. The proxy also forces
  // the tenant itself, so a browser-supplied tenant could not be trusted anyway.
  // Device identity is telemetry-scoped: the backend 401s a telemetry post
  // without it, and it must not leak onto reads.
  if (ctx.deviceSn) {
    h.set("X-Device-Sn", ctx.deviceSn);
  }
  // Correlate browser → service → logs. The backend echoes it back.
  h.set("X-Request-Id", newRequestId());
  if (hasBody && !h.has("Content-Type")) {
    h.set("Content-Type", "application/json");
  }
  h.set("Accept", "application/json");
  return h;
}

function newRequestId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function isAbort(err: unknown): boolean {
  return (
    typeof err === "object" &&
    err !== null &&
    (err as { name?: string }).name === "AbortError"
  );
}

/** Reject as soon as the caller aborts, so a poll loop cannot hang on a timer. */
function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/** Map a backend job `error_code` onto the finite client-side code set. */
function jobErrorCode(job: InvestigationJob): ApiErrorCode {
  const known: string[] = [
    "timeout",
    "unavailable",
    "execution_failed",
    "rate_limited",
    "invalid_input",
    "not_found",
    "denied",
    "ambiguous",
  ];
  const code = job.error_code ?? "";
  return known.includes(code) ? (code as ApiErrorCode) : "execution_failed";
}

// ---------------------------------------------------------------- request

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  ctx?: RequestContext;
  timeoutMs?: number;
  /** Caller-supplied signal; combined with the timeout signal. */
  signal?: AbortSignal;
  /** Long-task endpoints opt out of the default timeout. */
  longTask?: boolean;
  query?: Record<string, string | number | undefined>;
}

function withQuery(path: string, query?: RequestOptions["query"]): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

/**
 * Perform one API call and unwrap the envelope.
 * Throws ApiError / NetworkError / TimeoutError; returns the parsed body.
 */
export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const url = `${API_BASE_URL}${withQuery(path, opts.query)}`;
  const timeoutMs = opts.longTask
    ? INVESTIGATE_TIMEOUT_MS
    : (opts.timeoutMs ?? DEFAULT_TIMEOUT_MS);

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new TimeoutError()), timeoutMs);
  // Link the caller's signal (used to cancel an in-flight investigation).
  const onCallerAbort = () => controller.abort(opts.signal?.reason);
  if (opts.signal) {
    if (opts.signal.aborted) onCallerAbort();
    else opts.signal.addEventListener("abort", onCallerAbort, { once: true });
  }

  let res: Response;
  try {
    res = await fetch(url, {
      method: opts.method ?? "GET",
      headers: buildHeaders(opts.ctx ?? {}, undefined, opts.body !== undefined),
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: controller.signal,
      cache: "no-store",
    });
  } catch (err) {
    if (controller.signal.aborted) {
      const reason = controller.signal.reason;
      if (reason instanceof TimeoutError) throw reason;
      // Caller cancelled deliberately.
      throw new DOMException("Aborted", "AbortError");
    }
    throw new NetworkError("无法连接到服务（网络或跨域问题）", err);
  } finally {
    clearTimeout(timer);
    opts.signal?.removeEventListener("abort", onCallerAbort);
  }

  const requestId = res.headers.get("X-Request-Id") ?? undefined;

  // 204 or empty body: nothing to unwrap.
  if (res.status === 204) return undefined as T;

  let body: unknown;
  const text = await res.text();
  try {
    body = text ? JSON.parse(text) : undefined;
  } catch {
    // A non-JSON error page (e.g. an HTML 500 from a proxy, or a Next.js dev
    // 404 page). Never surface the raw markup to an operator — it is noise,
    // not a message. Collapse it to a short, honest description.
    const isHtml = /^\s*<(!doctype|html|head|body)/i.test(text);
    const detail = isHtml
      ? `服务返回了 HTML 页面而非 JSON（HTTP ${res.status}）`
      : text.slice(0, 200);
    if (!res.ok) {
      throw new ApiError({ code: "execution_failed", status: res.status, detail, requestId });
    }
    throw new ApiError({ code: "contract_mismatch", status: res.status, detail: "非 JSON 响应", requestId });
  }

  if (isErrorEnvelope(body)) {
    throw new ApiError({
      code: body.error,
      status: res.status || DEFAULT_STATUS[body.error] || 500,
      detail: body.detail,
      reason: body.reason,
      requestId,
    });
  }

  // A FastAPI HTTPException (the /v1/telemetry identity check) returns a bare
  // `{detail: "..."}` with no envelope — handle that second shape too (§2).
  if (!res.ok) {
    const detail = (body as { detail?: unknown } | undefined)?.detail;
    const code: ApiErrorCode = res.status === 401 ? "denied" : "execution_failed";
    throw new ApiError({ code, status: res.status, detail, requestId });
  }

  return body as T;
}

function isErrorEnvelope(body: unknown): body is ApiErrorEnvelope {
  return (
    typeof body === "object" &&
    body !== null &&
    (body as { ok?: unknown }).ok === false &&
    typeof (body as { error?: unknown }).error === "string"
  );
}

// ------------------------------------------------------------- typed calls

function pageQuery(params?: PageParams): RequestOptions["query"] {
  return { limit: params?.limit, offset: params?.offset };
}

/** Assert an envelope that claims `ok:true` also has the expected payload;
 *  a missing `items` array would otherwise blow up deep in a component. */
function requireArray<T>(page: Page<T>): Page<T> {
  if (!Array.isArray(page.items)) {
    throw new ApiError({ code: "contract_mismatch", status: 200, detail: "响应缺少 items 数组" });
  }
  return page;
}

export const api = {
  health(opts?: { signal?: AbortSignal }) {
    return request<import("./types").Healthz>("/healthz", { ...opts, timeoutMs: 5000 });
  },

  ready(opts?: { signal?: AbortSignal }) {
    // /readyz returns 503 when not ready; that is a valid answer, not an error
    // we should surface as a failure. Read the envelope regardless of status.
    return request<import("./types").Readyz>("/readyz", { ...opts, timeoutMs: 5000 });
  },

  /** Prometheus text. Returns raw text; the dashboard parses the lines it needs. */
  async metrics(opts?: { signal?: AbortSignal; ctx?: RequestContext }): Promise<string> {
    const res = await fetch(`${API_BASE_URL}/metrics`, {
      headers: buildHeaders(opts?.ctx ?? {}),
      signal: opts?.signal,
      cache: "no-store",
    });
    if (!res.ok) {
      throw new ApiError({
        code: res.status === 401 ? "denied" : "execution_failed",
        status: res.status,
        requestId: res.headers.get("X-Request-Id") ?? undefined,
      });
    }
    return res.text();
  },

  /** JSON summary (spec §3.7). Absent on the backend today → callers degrade. */
  summary(opts?: { signal?: AbortSignal }) {
    return request<import("./types").Summary>("/v1/summary", { ...opts, timeoutMs: 5000 });
  },

  listCandidates(
    filters: { device_sn?: string; component_ref?: string } = {},
    page?: PageParams,
    opts?: { signal?: AbortSignal },
  ) {
    return request<Page<import("./types").CandidateEvent>>("/v1/candidates", {
      query: { ...filters, ...pageQuery(page) },
      signal: opts?.signal,
    }).then(requireArray);
  },

  getCandidate(id: string, opts?: { signal?: AbortSignal }) {
    return request<{ ok: true; item: import("./types").CandidateEvent }>(
      `/v1/candidates/${encodeURIComponent(id)}`,
      opts,
    );
  },

  /** Spec §3.5 evidence endpoint. Backend stage D. */
  getCandidateEvidence(id: string, opts?: { signal?: AbortSignal }) {
    return request<import("./types").CandidateEvidence>(
      `/v1/candidates/${encodeURIComponent(id)}/evidence`,
      opts,
    );
  },

  /** Resolve this candidate's opaque evidence refs into readable evidence.
   *
   * Server-side this may read a raw log body, which is **audited**
   * (`action=rawlog.read`) — call it when the operator asks, not on render. */
  getCandidateRefs(id: string, opts?: { signal?: AbortSignal }) {
    return request<import("./types").CandidateRefs>(
      `/v1/candidates/${encodeURIComponent(id)}/refs`,
      opts,
    );
  },

  listAlerts(page?: PageParams, opts?: { signal?: AbortSignal }) {
    return request<Page<import("./types").AlertEvent>>("/v1/alerts", {
      query: pageQuery(page),
      signal: opts?.signal,
    }).then(requireArray);
  },

  listOutbox(
    filters: { status?: string } = {},
    page?: PageParams,
    opts?: { signal?: AbortSignal },
  ) {
    return request<Page<import("./types").OutboxRow>>("/v1/outbox", {
      query: { ...filters, ...pageQuery(page) },
      signal: opts?.signal,
    }).then(requireArray);
  },

  listReports(page?: PageParams, opts?: { signal?: AbortSignal }) {
    return request<Page<import("./types").InvestigationReport>>("/v1/reports", {
      query: pageQuery(page),
      signal: opts?.signal,
    }).then(requireArray);
  },

  getInvestigation(id: string, opts?: { signal?: AbortSignal }) {
    return request<{
      ok: true;
      investigation_id: string;
      report: import("./types").InvestigationReport;
    }>(`/v1/investigations/${encodeURIComponent(id)}`, opts);
  },

  /**
   * Submit → (poll →) fetch the assembled result. Handles **both** backend modes:
   *
   *  - **async** (`DD_INVESTIGATE_ASYNC=1`): POST returns `202 {job_id}`, the LLM
   *    work runs in a worker pool; poll `.../jobs/{id}` until terminal, then take
   *    `.../jobs/{id}/result` (report + verdict + trace + usage, the same shape the
   *    synchronous response always had).
   *  - **sync** (default today, or `?sync=1`): POST carries the whole result; return it.
   *
   * Cancelling aborts the signal: the poll stops immediately **and** we ask the
   * backend to cancel (best effort — the worker stops at its next step boundary, so
   * a report may already be persisted; the job status is the truth, not this call).
   */
  async investigate(
    candidateId: string,
    opts?: { signal?: AbortSignal; ctx?: RequestContext; pollMs?: number },
  ): Promise<InvestigationResult> {
    const signal = opts?.signal;
    const submitted = await request<
      InvestigationResult | { ok: true; job_id: string }
    >("/v1/investigations", {
      method: "POST",
      body: { candidate_id: candidateId },
      longTask: true,
      signal,
      ctx: opts?.ctx,
    });
    if (!("job_id" in submitted) || "report" in submitted) {
      // Synchronous mode: the POST already carries the whole result.
      return submitted as InvestigationResult;
    }
    const jobId = (submitted as { job_id: string }).job_id;
    const pollMs = opts?.pollMs ?? INVESTIGATE_POLL_MS;
    try {
      for (;;) {
        await sleep(pollMs, signal);
        const { job } = await request<{ ok: true; job: InvestigationJob }>(
          `/v1/investigations/jobs/${encodeURIComponent(jobId)}`,
          { signal },
        );
        if (job.status === "succeeded") {
          return await request<InvestigationResult>(
            `/v1/investigations/jobs/${encodeURIComponent(jobId)}/result`,
            { signal },
          );
        }
        if (job.status === "failed" || job.status === "cancelled") {
          throw new ApiError({
            code: jobErrorCode(job),
            status: job.status === "cancelled" ? 409 : 500,
            detail: job.error_code ?? job.status,
          });
        }
      }
    } catch (err) {
      if (isAbort(err)) {
        // Best effort: stop the worker too, but never mask the abort with its error.
        void request<unknown>(
          `/v1/investigations/jobs/${encodeURIComponent(jobId)}/cancel`,
          { method: "POST" },
        ).catch(() => undefined);
      }
      throw err;
    }
  },

  /** `GET /v1/investigations/jobs/{id}` — poll one async job. */
  getInvestigationJob(jobId: string, opts?: { signal?: AbortSignal }) {
    return request<{ ok: true; job: InvestigationJob }>(
      `/v1/investigations/jobs/${encodeURIComponent(jobId)}`,
      opts,
    );
  },

  /** `POST /v1/investigations/jobs/{id}/cancel` — cooperative cancel. */
  cancelInvestigationJob(jobId: string) {
    return request<{ ok: true; job: InvestigationJob }>(
      `/v1/investigations/jobs/${encodeURIComponent(jobId)}/cancel`,
      { method: "POST" },
    );
  },

  ingest(
    body: { events: unknown[] },
    ctx: RequestContext & { deviceSn: string },
    opts?: { signal?: AbortSignal },
  ) {
    return request<import("./types").IngestResult>("/v1/telemetry", {
      method: "POST",
      body,
      ctx,
      signal: opts?.signal,
      timeoutMs: 30_000,
    });
  },
};
