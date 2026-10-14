// @vitest-environment node
//
// The api client is pure network code with no DOM dependency. Running it under
// jsdom breaks msw: jsdom swaps in its own AbortController realm, and undici's
// Request constructor (called inside msw's interceptor) rejects a cross-realm
// signal with "Expected signal to be an instance of AbortSignal". Under node
// there is a single realm, so the signal passes and msw intercepts cleanly.
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { ApiError, api } from "@/lib/api/client";
import { humanize } from "@/lib/errors";
import { candidate, investigationResult } from "./fixtures";
import { server } from "./msw/server";

describe("API client — header injection", () => {
  it("sends X-Request-Id but **no credentials** (the server-side proxy owns those)", async () => {
    let seen: Headers | null = null;
    server.use(
      http.get("*/v1/candidates", ({ request }) => {
        seen = request.headers;
        return HttpResponse.json({
          ok: true,
          count: 0,
          total: 0,
          limit: 50,
          offset: 0,
          has_more: false,
          items: [],
        });
      }),
    );

    await api.listCandidates();

    expect(seen).not.toBeNull();
    expect(seen!.get("X-Request-Id")).toMatch(/[0-9a-f-]{8,}/i);
    // 安全属性：浏览器侧**绝不**携带密钥或租户。`NEXT_PUBLIC_*` 会被 Next 内联进
    // 客户端包，任何人打开 devtools 就能读到；租户由服务端代理强制指定。
    expect(seen!.get("X-API-Key")).toBeNull();
    expect(seen!.get("X-Tenant-Id")).toBeNull();
  });

  it("does not put identity in the request body", async () => {
    let body: unknown = null;
    server.use(
      http.post("*/v1/telemetry", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({
          ok: true,
          stats: { ingested: 0, resolved: 0, rejected: 0, rejected_by_reason: {}, normalized: 0, candidates: 0 },
          persisted: { telemetry: 0, candidates: 0 },
          candidates: [],
        });
      }),
    );

    await api.ingest({ events: [{ event_id: "e1" }] }, { deviceSn: "d1" });

    expect(JSON.stringify(body)).not.toContain("tenant");
    expect(JSON.stringify(body)).not.toContain("deviceSn");
  });

  it("sends X-Device-Sn only on telemetry", async () => {
    const headers: Record<string, Headers> = {};
    server.use(
      http.post("*/v1/telemetry", ({ request }) => {
        headers.telemetry = request.headers;
        return HttpResponse.json({ ok: true, stats: {}, persisted: {}, candidates: [] });
      }),
      http.get("*/v1/candidates", ({ request }) => {
        headers.candidates = request.headers;
        return HttpResponse.json({ ok: true, count: 0, total: 0, limit: 50, offset: 0, has_more: false, items: [] });
      }),
    );

    await api.ingest({ events: [{ event_id: "e1" }] }, { deviceSn: "device-xyz" });
    await api.listCandidates();

    expect(headers.telemetry!.get("X-Device-Sn")).toBe("device-xyz");
    expect(headers.candidates!.get("X-Device-Sn")).toBeNull();
  });
});

describe("API client — error classification", () => {
  it("classifies a {ok:false} envelope into a typed ApiError by code", async () => {
    server.use(
      http.get("*/v1/candidates", () =>
        HttpResponse.json(
          { ok: false, error: "denied", detail: "缺少 API Key", reason: "unauthorized" },
          { status: 401 },
        ),
      ),
    );

    await expect(api.listCandidates()).rejects.toMatchObject({
      name: "ApiError",
      code: "denied",
      status: 401,
    });
  });

  it("renders a 404 as 'not found', not 'no permission'", async () => {
    server.use(
      http.get("*/v1/candidates/:id", () =>
        HttpResponse.json({ ok: false, error: "not_found" }, { status: 404 }),
      ),
    );

    try {
      await api.getCandidate("cand_x");
      throw new Error("should have thrown");
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).isNotFound).toBe(true);
      const h = humanize(err);
      expect(h.title).toBe("未找到");
      expect(h.title).not.toContain("权限");
    }
  });

  it("handles the bare FastAPI {detail} shape (the telemetry identity 401)", async () => {
    server.use(
      http.post("*/v1/telemetry", () =>
        HttpResponse.json({ detail: "缺少可信身份：需要请求头 X-Tenant-Id 与 X-Device-Sn" }, { status: 401 }),
      ),
    );

    try {
      await api.ingest({ events: [{ event_id: "e1" }] }, { deviceSn: "d" });
      throw new Error("should have thrown");
    } catch (err) {
      expect((err as ApiError).code).toBe("denied");
      expect((err as ApiError).status).toBe(401);
    }
  });

  it("treats 207 persist_failed as a distinct, non-fatal-ish code", async () => {
    server.use(
      http.post("*/v1/telemetry", () =>
        HttpResponse.json(
          { ok: false, error: "persist_failed", stats: {}, candidates: [] },
          { status: 207 },
        ),
      ),
    );

    try {
      await api.ingest({ events: [{ event_id: "e1" }] }, { deviceSn: "d" });
      throw new Error("should have thrown");
    } catch (err) {
      expect((err as ApiError).code).toBe("persist_failed");
      expect((err as ApiError).status).toBe(207);
      expect(humanize(err).title).toContain("没有存住");
    }
  });

  it("surfaces a non-JSON error page as an ApiError, not a crash", async () => {
    server.use(
      http.get("*/v1/candidates", () => HttpResponse.html("<html>502 Bad Gateway</html>", { status: 502 })),
    );
    await expect(api.listCandidates()).rejects.toBeInstanceOf(ApiError);
  });

  it("never dumps raw HTML into the operator-facing detail", async () => {
    server.use(
      http.get("*/v1/candidates", () => HttpResponse.html("<html>502 Bad Gateway</html>", { status: 502 })),
    );
    try {
      await api.listCandidates();
      throw new Error("should have thrown");
    } catch (err) {
      const detail = String((err as ApiError).detail);
      expect(detail).not.toContain("<html>");
      expect(detail).toContain("HTML");
    }
  });
});

describe("API client — pagination + validation", () => {
  it("parses the pagination envelope", async () => {
    const page = await api.listCandidates({}, { limit: 20, offset: 0 });
    expect(page).toMatchObject({ count: 1, total: 1, limit: 20, offset: 0, has_more: false });
    expect(page.items[0]?.candidate_id).toBe("cand_ab12cd34ef56");
  });

  it("throws contract_mismatch when a claimed-ok body lacks items", async () => {
    server.use(http.get("*/v1/candidates", () => HttpResponse.json({ ok: true, total: 3 })));
    try {
      await api.listCandidates();
      throw new Error("should have thrown");
    } catch (err) {
      expect((err as ApiError).code).toBe("contract_mismatch");
    }
  });

  it("passes filters and paging as query params", async () => {
    let url: URL | null = null;
    server.use(
      http.get("*/v1/candidates", ({ request }) => {
        url = new URL(request.url);
        return HttpResponse.json({ ok: true, count: 0, total: 0, limit: 20, offset: 20, has_more: false, items: [] });
      }),
    );

    await api.listCandidates({ component_ref: "joint_motor.front_left_hip" }, { limit: 20, offset: 20 });
    expect(url!.searchParams.get("component_ref")).toBe("joint_motor.front_left_hip");
    expect(url!.searchParams.get("limit")).toBe("20");
    expect(url!.searchParams.get("offset")).toBe("20");
  });
});

describe("API client — long task cancellation", () => {
  it("aborts an in-flight investigation when the caller's signal fires", async () => {
    server.use(
      http.post("*/v1/investigations", async () => {
        await new Promise((r) => setTimeout(r, 5000));
        return HttpResponse.json({ ok: true, investigation_id: "x" });
      }),
    );

    const controller = new AbortController();
    const p = api.investigate("cand_ab12cd34ef56", { signal: controller.signal });
    setTimeout(() => controller.abort(), 20);

    await expect(p).rejects.toMatchObject({ name: "AbortError" });
  });
});

describe("API client — async investigation (submit → poll → result)", () => {
  it("polls the job, then reads the assembled result (report+verdict+trace+usage)", async () => {
    const seen: string[] = [];
    let polls = 0;
    server.use(
      http.post("*/v1/investigations", () =>
        HttpResponse.json({ ok: true, job_id: "job_1", status: "pending" }, { status: 202 }),
      ),
      http.get("*/v1/investigations/jobs/job_1", () => {
        polls += 1;
        seen.push("poll");
        return HttpResponse.json({
          ok: true,
          job: {
            job_id: "job_1",
            candidate_id: candidate.candidate_id,
            status: polls < 2 ? "running" : "succeeded",
            report_id: investigationResult.investigation_id,
            error_code: null,
          },
        });
      }),
      http.get("*/v1/investigations/jobs/job_1/result", () => {
        seen.push("result");
        return HttpResponse.json(investigationResult);
      }),
    );

    const out = await api.investigate(candidate.candidate_id, { pollMs: 1 });
    expect(seen).toEqual(["poll", "poll", "result"]);
    expect(out.investigation_id).toBe(investigationResult.investigation_id);
    expect(out.verdict).toBeTruthy();
    expect(out.trace.length).toBeGreaterThan(0);
  });

  it("returns the synchronous response as-is when the backend is not in async mode", async () => {
    server.use(http.post("*/v1/investigations", () => HttpResponse.json(investigationResult)));
    const out = await api.investigate("cand_ab12cd34ef56");
    expect(out.investigation_id).toBe(investigationResult.investigation_id);
  });

  it("turns a failed job into an ApiError carrying the backend error_code", async () => {
    server.use(
      http.post("*/v1/investigations", () =>
        HttpResponse.json({ ok: true, job_id: "job_2", status: "pending" }, { status: 202 }),
      ),
      http.get("*/v1/investigations/jobs/job_2", () =>
        HttpResponse.json({
          ok: true,
          job: {
            job_id: "job_2",
            candidate_id: "cand_x",
            status: "failed",
            report_id: null,
            error_code: "timeout",
          },
        }),
      ),
    );

    await expect(api.investigate("cand_x", { pollMs: 1 })).rejects.toMatchObject({
      name: "ApiError",
      code: "timeout",
    });
  });

  it("stops polling and asks the backend to cancel when the caller aborts", async () => {
    let cancelled = false;
    server.use(
      http.post("*/v1/investigations", () =>
        HttpResponse.json({ ok: true, job_id: "job_3", status: "pending" }, { status: 202 }),
      ),
      http.get("*/v1/investigations/jobs/job_3", () =>
        HttpResponse.json({
          ok: true,
          job: {
            job_id: "job_3",
            candidate_id: "cand_x",
            status: "running",
            report_id: null,
            error_code: null,
          },
        }),
      ),
      http.post("*/v1/investigations/jobs/job_3/cancel", () => {
        cancelled = true;
        return HttpResponse.json({ ok: true, job: { job_id: "job_3", status: "cancelled" } });
      }),
    );

    const controller = new AbortController();
    const p = api.investigate("cand_x", { signal: controller.signal, pollMs: 1 });
    setTimeout(() => controller.abort(), 15);
    await expect(p).rejects.toMatchObject({ name: "AbortError" });
    // The cancel is fired best-effort (not awaited) so the abort is not delayed.
    await new Promise((r) => setTimeout(r, 25));
    expect(cancelled).toBe(true);
  });
});
