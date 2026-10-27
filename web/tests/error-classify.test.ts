import { describe, expect, it } from "vitest";

import { ApiError, NetworkError, TimeoutError } from "@/lib/api/client";
import type { ApiErrorCode } from "@/lib/api/types";
import { classify, waitPhrase } from "@/lib/error-classify";
import { humanize } from "@/lib/errors";
import { describeRuntimeError } from "@/lib/hints";

/**
 * 守卫：控制台有两套错误渲染器（`lib/errors.ts` 给页面用，`lib/hints.ts` 给错误页用），
 * 但它们必须对同一个失败给出**同一个判断**。
 *
 * 真实事故：同一个 429，列表页说"请稍等几秒"、错误页说"约 5 秒后"。服务端明明发了
 * `Retry-After: 5`，其中一个渲染器把它丢了。用户看到哪个说法，取决于哪个组件先出错。
 * 措辞可以不同（大面板要一句话，小卡片要三个字），**判断不能不同**。
 */

const CODES: ApiErrorCode[] = [
  "denied",
  "incompatible_binding",
  "invalid_input",
  "not_found",
  "timeout",
  "unavailable",
  "execution_failed",
  "contract_mismatch",
  "ambiguous",
  "version_conflict",
  "rate_limited",
  "persist_failed",
];

const STATUS: Partial<Record<ApiErrorCode, number>> = {
  denied: 403,
  invalid_input: 422,
  not_found: 404,
  timeout: 504,
  unavailable: 503,
  execution_failed: 500,
  rate_limited: 429,
};

describe("错误判断只有一个来源", () => {
  it("两套渲染器对每种错误码的 retryable 完全一致", () => {
    for (const code of CODES) {
      const err = new ApiError({ code, status: STATUS[code] ?? 400 });
      const viaPages = humanize(err).retryable;
      const viaBoundary = classify(err).retryable;
      expect(viaPages, `code=${code} 两套说法不一致`).toBe(viaBoundary);
      // 错误页自身没有 retryable 字段（它由 error.tsx 的 reset 按钮决定），
      // 所以这里校验的是它依赖的 classify()。
      expect(describeRuntimeError({ code, status: STATUS[code] }).title).toBeTruthy();
    }
  });

  it("确定性拒绝不允许重试（重试只会浪费操作员时间）", () => {
    for (const code of ["denied", "not_found", "invalid_input"] as ApiErrorCode[]) {
      expect(classify(new ApiError({ code, status: STATUS[code] ?? 400 })).retryable, code).toBe(
        false,
      );
    }
  });

  it("限流时两套渲染器都使用服务端给的秒数", () => {
    const err = new ApiError({ code: "rate_limited", status: 429, retryAfterSeconds: 7 });
    expect(humanize(err).hint).toContain("约 7 秒后");
    expect(describeRuntimeError({ code: "rate_limited", status: 429, retryAfterSeconds: 7 }).action)
      .toContain("约 7 秒后");
  });

  it("服务端没给秒数时，两套渲染器都不编数字", () => {
    const err = new ApiError({ code: "rate_limited", status: 429 });
    expect(humanize(err).hint).not.toMatch(/\d+\s*秒/);
    expect(describeRuntimeError({ code: "rate_limited", status: 429 }).action).not.toMatch(
      /\d+\s*秒/,
    );
    expect(waitPhrase(undefined)).toBe("稍后");
  });

  it("客户端侧失败（超时/断网）两套渲染器都判为可重试", () => {
    expect(classify(new TimeoutError()).category).toBe("timeout");
    expect(classify(new NetworkError("offline")).category).toBe("network");
    expect(humanize(new TimeoutError()).retryable).toBe(true);
    expect(humanize(new NetworkError("offline")).retryable).toBe(true);
    // 错误页收到的不是 Error 实例，而是被 Next 序列化过的普通对象
    expect(describeRuntimeError({ name: "NetworkError" }).title).toContain("连不上");
    expect(describeRuntimeError({ name: "TimeoutError" }).title).toContain("超时");
  });

  it("分类与真实 HTTP 语义对齐（401/403→denied、404→not_found、5xx→server_error）", () => {
    expect(classify(new ApiError({ code: "denied", status: 401 })).category).toBe("denied");
    expect(classify(new ApiError({ code: "not_found", status: 404 })).category).toBe("not_found");
    expect(classify(new ApiError({ code: "execution_failed", status: 502 })).category).toBe(
      "server_error",
    );
  });
});
