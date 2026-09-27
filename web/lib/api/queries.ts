"use client";

/**
 * TanStack Query hooks. One place that knows each endpoint's caching and
 * retry policy, so components stay declarative.
 *
 * Retry policy follows the error taxonomy: only retry transient failures
 * (timeout / unavailable / network / rate_limited). Never retry `denied`,
 * `invalid_input`, `not_found`, or `incompatible_binding` — retrying a
 * deterministic rejection just wastes the operator's time.
 */

import {
  useMutation,
  useQuery,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { ApiError, NetworkError, TimeoutError, api } from "./client";
import type {
  AlertEvent,
  CandidateEvent,
  CandidateEvidence,
  InvestigationReport,
  InvestigationResult,
  OutboxRow,
  PageParams,
  Summary,
} from "./types";

function isRetryable(err: unknown): boolean {
  if (err instanceof TimeoutError || err instanceof NetworkError) return true;
  if (err instanceof ApiError) {
    return ["timeout", "unavailable", "rate_limited", "execution_failed"].includes(err.code);
  }
  return false;
}

const READ_OPTS = {
  staleTime: 10_000,
  retry: (count: number, err: unknown) => count < 2 && isRetryable(err),
};

// ------------------------------------------------------------------ probes

export function useReady(): UseQueryResult<import("./types").Readyz, unknown> {
  return useQuery({
    queryKey: ["readyz"],
    queryFn: ({ signal }) => api.ready({ signal }),
    refetchInterval: 15_000,
    ...READ_OPTS,
  });
}

export function useMetrics(): UseQueryResult<string, unknown> {
  return useQuery({
    queryKey: ["metrics"],
    queryFn: ({ signal }) => api.metrics({ signal }),
    refetchInterval: 15_000,
    ...READ_OPTS,
  });
}

/** Optional JSON summary (spec §3.7). If the backend lacks it the query
 *  errors and callers fall back to the parsed /metrics text. */
export function useSummary(): UseQueryResult<Summary, unknown> {
  return useQuery({
    queryKey: ["summary"],
    queryFn: ({ signal }) => api.summary({ signal }),
    retry: false,
    refetchInterval: 15_000,
  });
}

// -------------------------------------------------------------- candidates

export function useCandidates(
  filters: { device_sn?: string; component_ref?: string },
  page: PageParams,
): UseQueryResult<import("./types").Page<CandidateEvent>, unknown> {
  return useQuery({
    queryKey: ["candidates", filters, page],
    queryFn: ({ signal }) => api.listCandidates(filters, page, { signal }),
    placeholderData: (prev) => prev,
    ...READ_OPTS,
  });
}

export function useCandidate(
  id: string | null,
  opts?: { enabled?: boolean },
): UseQueryResult<CandidateEvent, unknown> {
  return useQuery({
    queryKey: ["candidate", id],
    queryFn: async ({ signal }) => (await api.getCandidate(id as string, { signal })).item,
    enabled: opts?.enabled !== false && !!id,
    ...READ_OPTS,
  });
}

export function useCandidateEvidence(
  id: string | null,
  opts?: { enabled?: boolean },
): UseQueryResult<CandidateEvidence, unknown> {
  return useQuery({
    queryKey: ["candidate-evidence", id],
    queryFn: ({ signal }) => api.getCandidateEvidence(id as string, { signal }),
    enabled: opts?.enabled !== false && !!id,
    ...READ_OPTS,
    retry: false, // endpoint may not exist yet (backend stage D) → show empty
  });
}

// ------------------------------------------------------------------ alerts

export function useAlerts(page: PageParams): UseQueryResult<import("./types").Page<AlertEvent>, unknown> {
  return useQuery({
    queryKey: ["alerts", page],
    queryFn: ({ signal }) => api.listAlerts(page, { signal }),
    placeholderData: (prev) => prev,
    ...READ_OPTS,
  });
}

export function useOutbox(
  filters: { status?: string },
  page: PageParams,
): UseQueryResult<import("./types").Page<OutboxRow>, unknown> {
  return useQuery({
    queryKey: ["outbox", filters, page],
    queryFn: ({ signal }) => api.listOutbox(filters, page, { signal }),
    placeholderData: (prev) => prev,
    ...READ_OPTS,
  });
}

// ----------------------------------------------------------------- reports

export function useReports(
  page: PageParams,
): UseQueryResult<import("./types").Page<InvestigationReport>, unknown> {
  return useQuery({
    queryKey: ["reports", page],
    queryFn: ({ signal }) => api.listReports(page, { signal }),
    placeholderData: (prev) => prev,
    retry: false, // backend stage D
  });
}

// --------------------------------------------------------------- hooks EOF

// ----------------------------------------------------------- investigation

/**
 * The core action. This is a synchronous LLM call on the backend (10–60s),
 * so:
 *  - it is a mutation, not a polling query (spec: do NOT auto-poll);
 *  - the caller owns the AbortController and passes its signal so the button
 *    can cancel.
 */
export function useInvestigate(): UseMutationResult<
  InvestigationResult,
  unknown,
  { candidateId: string; signal: AbortSignal }
> {
  return useMutation({
    mutationFn: ({ candidateId, signal }) => api.investigate(candidateId, { signal }),
    // No cache writer on purpose: the investigate page keeps the result in
    // local state. Persistence is the backend's job (it writes a report row).
    retry: false,
  });
}
