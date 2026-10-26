import { describe, expect, it } from "vitest";

import { ApiError, parseRetryAfter, request } from "@/lib/api/client";
import { describeRuntimeError } from "@/lib/hints";

/**
 * 目的：`Retry-After` 是**服务端已经知道的答案**，界面必须原样用它。
 * 后端在三个限流点分别回 5s / 10s / 60s；如果前端硬编码一个数字，就会出现
 * "服务端说等 5 秒、界面说等 60 秒"——操作员于是白等 55 秒，或者以为服务坏了。
 *
 * 这些用例同时钉住另一半：**服务端没给就不要编**（说"稍后"，不编秒数）。
 */
describe("parseRetryAfter", () => {
  it("读整数秒（后端实际使用的形式）", () => {
    expect(parseRetryAfter("5")).toBe(5);
    expect(parseRetryAfter("60")).toBe(60);
    expect(parseRetryAfter(" 10 ")).toBe(10);
  });

  it("缺失或非法就返回 undefined，绝不猜", () => {
    expect(parseRetryAfter(null)).toBeUndefined();
    expect(parseRetryAfter("")).toBeUndefined();
    expect(parseRetryAfter("   ")).toBeUndefined();
    expect(parseRetryAfter("soon")).toBeUndefined();
    expect(parseRetryAfter("Wed, 21 Oct 2015 07:28:00 GMT")).toBeUndefined();
    expect(parseRetryAfter("-5")).toBeUndefined();
    expect(parseRetryAfter("5.5")).toBeUndefined();
  });
});

describe("ApiError 把服务端的等待时间带到界面", () => {
  it("携带 retryAfterSeconds", () => {
    const err = new ApiError({
      code: "rate_limited",
      status: 429,
      retryAfterSeconds: 5,
    });
    expect(err.retryAfterSeconds).toBe(5);
    const hint = describeRuntimeError({
      name: err.name,
      message: err.message,
      status: err.status,
      code: err.code,
      retryAfterSeconds: err.retryAfterSeconds,
    });
    expect(hint.action).toContain("约 5 秒后");
    expect(hint.action).not.toContain("60");
  });

  it("服务端没给时，界面不下结论（说“稍后”，不编秒数）", () => {
    const err = new ApiError({ code: "rate_limited", status: 429 });
    expect(err.retryAfterSeconds).toBeUndefined();
    const hint = describeRuntimeError({
      name: err.name,
      message: err.message,
      status: err.status,
      code: err.code,
      retryAfterSeconds: err.retryAfterSeconds,
    });
    expect(hint.action).toContain("稍后再试");
    expect(hint.action).not.toMatch(/\d+\s*秒/);
  });
});

/**
 * 承重用例：真正走一遍 `request()`，钉住**三个抛错点都带上了这个头**。
 * 没有这条，前两条只能证明“解析和展示是对的”，但漏了“fetch 没把头上带”。
 */
describe("fetch 路径：429 + Retry-After 头 → ApiError.retryAfterSeconds", () => {
  it("从响应头读出来，而不是靠猜", async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response(
        JSON.stringify({ ok: false, error: "rate_limited", detail: "too many" }),
        {
          status: 429,
          headers: { "Content-Type": "application/json", "Retry-After": "5" },
        },
      )) as typeof fetch;
    try {
      await expect(
        request("/v1/investigations", { method: "POST", body: {} }),
      ).rejects.toMatchObject({
        code: "rate_limited",
        status: 429,
        retryAfterSeconds: 5,
      });
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it("没这个头时不伪造：undefined", async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response(
        JSON.stringify({ ok: false, error: "rate_limited", detail: "too many" }),
        {
          status: 429,
          headers: { "Content-Type": "application/json" },
        },
      )) as typeof fetch;
    try {
      await expect(
        request("/v1/investigations", { method: "POST", body: {} }),
      ).rejects.toMatchObject({
        code: "rate_limited",
        retryAfterSeconds: undefined,
      });
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});
