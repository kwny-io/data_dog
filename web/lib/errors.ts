/**
 * Turn a typed ApiError into something an operator can act on.
 *
 * The rule (spec §5): say what happened and what to do, in the interface's
 * voice. No apology, no vagueness. We branch on the finite `error` code and
 * never render the server's `detail` verbatim as the primary message —
 * `detail` is shown only as secondary, technical context.
 */

import { ApiError } from "./api/client";
import type { ApiErrorCode } from "./api/types";
import { classify, waitPhrase } from "./error-classify";

export interface HumanError {
  /** Short, plain-language headline. */
  title: string;
  /** One sentence: what to do next. */
  hint: string;
  /** Whether retrying is worth offering. */
  retryable: boolean;
  /** The raw code, shown small for support/correlation. */
  code: ApiErrorCode | "network" | "timeout";
}

const MESSAGES: Record<ApiErrorCode, Omit<HumanError, "code">> = {
  denied: {
    title: "没有访问权限",
    hint: "检查 API Key 是否正确，或这个账号是否属于当前租户。",
    retryable: false,
  },
  incompatible_binding: {
    title: "这条日志无法确定解析方式",
    hint: "数据被安全地拒收了（拿不准就拒绝）。请补充该设备的协议版本或固件信息。",
    retryable: false,
  },
  invalid_input: {
    title: "请求内容不合法",
    hint: "请检查填写的参数是否符合要求（例如事件数量、分页范围）。",
    retryable: false,
  },
  not_found: {
    title: "未找到",
    hint: "该资源不存在，或不属于当前租户。可以返回列表重新选择。",
    retryable: false,
  },
  timeout: {
    title: "调查超时",
    hint: "模型调用超过了时限。可以重试，或稍后在报告列表里查看是否已生成。",
    retryable: true,
  },
  unavailable: {
    title: "服务暂时不可用",
    hint: "上游依赖（数据库或模型）未就绪。稍后重试。",
    retryable: true,
  },
  execution_failed: {
    title: "服务内部错误",
    hint: "这是未预期的异常。记下请求 ID 便于排查，稍后重试。",
    retryable: true,
  },
  contract_mismatch: {
    title: "响应格式与前端预期不一致",
    hint: "后端可能已升级契约。请核对接口版本，或反馈这个问题。",
    retryable: false,
  },
  ambiguous: {
    title: "存在多个匹配，无法唯一确定",
    hint: "请补充更具体的筛选条件（例如更精确的设备序列号或部件）。",
    retryable: false,
  },
  version_conflict: {
    title: "数据已被其他人修改",
    hint: "刷新后重试，你的改动不会覆盖对方的改动。",
    retryable: true,
  },
  rate_limited: {
    title: "请求过于频繁",
    hint: "已触发限流。请稍等几秒再试，不要并发重试。",
    retryable: true,
  },
  persist_failed: {
    title: "数据算出来了，但没有存住",
    hint: "解析成功但写库失败。本次结果仅供临时查看，请排查数据库后重新提交。",
    retryable: true,
  },
};

export function humanize(err: unknown): HumanError {
  // The category and the retry decision come from `error-classify` — the single
  // judge both this module and `hints.ts` answer to. This module only supplies
  // the console's wording for that category.
  const c = classify(err);
  if (c.category === "timeout") {
    return {
      title: "请求超时",
      hint: "服务在时限内没有响应。请检查网络后重试。",
      retryable: true,
      code: "timeout",
    };
  }
  if (c.category === "network") {
    return {
      title: "无法连接到服务",
      hint: "服务可能未启动，或本地开发时跨域被拦截（需后端开启 CORS）。",
      retryable: true,
      code: "network",
    };
  }
  if (err instanceof ApiError) {
    const base = MESSAGES[err.code] ?? MESSAGES.execution_failed;
    // A 429 carries the server's own Retry-After; dropping it here is what made
    // this page say "请稍等几秒" while the error boundary said "约 5 秒后".
    const hint =
      c.category === "rate_limited"
        ? `已触发限流。${waitPhrase(c.waitSeconds)}再试即可，不要并发重试。`
        : base.hint;
    return { ...base, hint, retryable: c.retryable, code: err.code };
  }
  return { ...MESSAGES.execution_failed, code: "execution_failed" };
}

/** Secondary technical line: request id + server detail, for support. */
export function technicalLine(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null;
  const parts: string[] = [];
  if (err.requestId) parts.push(`请求 ID ${err.requestId}`);
  if (err.detail != null) {
    const d = typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail);
    if (d) parts.push(d.slice(0, 240));
  }
  return parts.length ? parts.join(" · ") : null;
}
