import { http, HttpResponse } from "msw";

import { alertPage, candidate, candidatePage, investigationResult } from "../fixtures";

// A path-only pattern ("/v1/candidates") matches in a browser (resolved against
// location.origin) but NOT in the node test environment, where the request URL
// is absolute ("http://localhost:8000/v1/candidates"). The wildcard origin keeps
// these defaults matching under both.
const BASE = "*";

/** Default happy-path handlers. Tests override with server.use(...) for
 *  error/edge cases, and assert on the captured headers. */
export const handlers = [
  http.get(`${BASE}/readyz`, () =>
    HttpResponse.json({ status: "ready", checks: { database: "ok", migration: "ok", resolver: "ok" } }),
  ),

  http.get(`${BASE}/metrics`, () =>
    HttpResponse.text(
      [
        "# HELP data_dog_db_rows rows in db",
        'data_dog_db_rows{table="candidates"} 137',
        "data_dog_uptime_seconds 3600",
        'data_dog_http_requests_total{method="POST",path="/v1/telemetry",status="200"} 12',
        'data_dog_http_requests_total{method="GET",path="/v1/candidates",status="200"} 40',
      ].join("\n"),
      { headers: { "Content-Type": "text/plain; version=0.0.4" } },
    ),
  ),

  http.get(`${BASE}/v1/candidates`, () => HttpResponse.json(candidatePage)),

  http.get(`${BASE}/v1/candidates/:id`, ({ params }) =>
    HttpResponse.json({ ok: true, item: { ...candidate, candidate_id: String(params.id) } }),
  ),

  http.get(`${BASE}/v1/candidates/:id/evidence`, () =>
    HttpResponse.json({
      ok: false,
      error: "not_found",
      detail: "endpoint not implemented",
    }, { status: 404 }),
  ),

  http.get(`${BASE}/v1/alerts`, () => HttpResponse.json(alertPage)),

  http.get(`${BASE}/v1/outbox`, () =>
    HttpResponse.json({ ok: true, count: 0, total: 0, limit: 50, offset: 0, has_more: false, items: [] }),
  ),

  http.post(`${BASE}/v1/investigations`, () => HttpResponse.json(investigationResult)),
];
