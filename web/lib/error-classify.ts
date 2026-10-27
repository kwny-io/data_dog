/**
 * The ONE place that decides what an error *is*.
 *
 * Why this module exists: the console had two independent translators
 * (`lib/errors.ts` for the pages, `lib/hints.ts` for the error boundaries) and
 * they disagreed about the same failure. A 429 rendered as "请稍等几秒" on a
 * list page and as "约 5 秒后" on the boundary — the server had sent
 * `Retry-After: 5`, and one of the two surfaces threw that information away.
 * Two translators will always drift; the wording may differ per surface (a
 * panel needs a sentence, a tile needs three words), but the **judgement**
 * — which category, may it be retried, how long must the operator wait —
 * must have exactly one answer.
 *
 * So: this module owns the judgement and no copy. `lib/errors.ts` and
 * `lib/hints.ts` are pure renderers over `classify()`.
 * `tests/error-classify.test.ts` pins that both renderers agree.
 */

import { ApiError, NetworkError, TimeoutError } from "./api/client";

/** The finite set of situations the UI knows how to explain. */
export type ErrorCategory =
  | "timeout"
  | "network"
  | "rate_limited"
  | "denied"
  | "not_found"
  | "unavailable"
  | "server_error"
  | "unknown";

export interface Classification {
  category: ErrorCategory;
  /** Whether offering a retry button is honest (the failure is transient). */
  retryable: boolean;
  /** Seconds the server asked us to wait; undefined when it did not say. */
  waitSeconds?: number;
  /** HTTP status when there was one (client-side failures have none). */
  status?: number;
}

/** Retrying a deterministic rejection just wastes the operator's time. */
const NON_RETRYABLE = new Set<ErrorCategory>(["denied", "not_found"]);

function categoryFrom(status: number | undefined, code: string): ErrorCategory {
  if (status === 429 || code === "rate_limited") return "rate_limited";
  if (status === 401 || status === 403 || code === "denied") return "denied";
  if (status === 404 || code === "not_found") return "not_found";
  if (status === 503) return "unavailable";
  if (status !== undefined && status >= 500) return "server_error";
  return "unknown";
}

/** Classify anything the API layer can throw. Never throws itself. */
export function classify(err: unknown): Classification {
  if (err instanceof TimeoutError) {
    return { category: "timeout", retryable: true };
  }
  if (err instanceof NetworkError) {
    return { category: "network", retryable: true };
  }
  if (err instanceof ApiError) {
    const category = categoryFrom(err.status, err.code);
    return {
      category,
      retryable: !NON_RETRYABLE.has(category) && err.code !== "invalid_input",
      waitSeconds: err.retryAfterSeconds,
      status: err.status,
    };
  }
  // Error-boundary props arrive as plain objects, not class instances.
  if (typeof err === "object" && err !== null) {
    const e = err as {
      name?: string;
      status?: number;
      code?: string;
      retryAfterSeconds?: number;
    };
    if (e.name === "TimeoutError") return { category: "timeout", retryable: true };
    if (e.name === "NetworkError") return { category: "network", retryable: true };
    const code = (e.code ?? "").toLowerCase();
    const status = typeof e.status === "number" ? e.status : undefined;
    const category = categoryFrom(status, code);
    return {
      category,
      retryable: !NON_RETRYABLE.has(category) && code !== "invalid_input",
      waitSeconds: typeof e.retryAfterSeconds === "number" ? e.retryAfterSeconds : undefined,
      status,
    };
  }
  return { category: "unknown", retryable: false };
}

/** "约 5 秒" / "稍后" — both surfaces must use this, so they cannot disagree. */
export function waitPhrase(waitSeconds: number | undefined): string {
  return waitSeconds && waitSeconds > 0 ? `约 ${waitSeconds} 秒后` : "稍后";
}
