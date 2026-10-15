// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "@/lib/api/client";

/** `GET /v1/candidates/{id}/refs` 客户端：路径、信封、以及"失败的引用不抛异常"。 */

afterEach(() => vi.unstubAllGlobals());

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api.getCandidateRefs", () => {
  it("打 /v1/candidates/{id}/refs，并把逐条结果（含失败项）原样带回", async () => {
    const spy = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({
        ok: true,
        candidate_id: "cand-1",
        resolved: 1,
        total: 2,
        results: [
          { ok: true, source: "rawlog",
            ref: { scheme: "rawlog", authority: "evt-1", path: [] },
            payload: { content: "body" } },
          { ok: false,
            ref: { scheme: "metricwin", authority: "cand-1", path: [] },
            error: "not_found" },
        ],
      }),
    );
    vi.stubGlobal("fetch", spy);

    const out = await api.getCandidateRefs("cand-1");
    expect(out.resolved).toBe(1);
    expect(out.total).toBe(2);
    expect(out.results[0]?.payload?.content).toBe("body");
    expect(out.results[1]?.error).toBe("not_found");
    expect(String(spy.mock.calls[0]?.[0])).toContain("/v1/candidates/cand-1/refs");
  });

  it("整个请求失败 → ApiError（调用方按 code 分支，不解析 detail）", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      jsonResponse({ ok: false, error: "invalid_input", detail: "bad ref" }, 400)));
    await expect(api.getCandidateRefs("cand-1")).rejects.toBeInstanceOf(ApiError);
  });
});
