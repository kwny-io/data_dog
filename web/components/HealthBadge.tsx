"use client";

import { useReady } from "@/lib/api/queries";
import { humanize } from "@/lib/errors";

/**
 * Live service-readiness badge, driven by GET /readyz.
 *
 * /readyz returns 503 when not ready and 200 when ready; both are legitimate
 * answers. Because the backend marks readiness non-ready by *status code*,
 * the fetch layer surfaces 503 as an ApiError with code "unavailable" — so we
 * treat that specific case as "not_ready" rather than as a hard failure.
 */
export function HealthBadge() {
  const { data, isPending, isError, error } = useReady();

  let dot: "ok" | "warn" | "bad" | "idle" = "idle";
  let label = "检测中…";
  let title = "正在读取 /readyz";

  if (isPending) {
    dot = "idle";
  } else if (data) {
    if (data.status === "ready") {
      dot = "ok";
      label = "就绪";
      title = `服务就绪 · 检查项：${Object.keys(data.checks ?? {}).join("、") || "无"}`;
    } else {
      dot = "warn";
      label = "未就绪";
      title = `服务未就绪 · 检查项：${JSON.stringify(data.checks ?? {})}`;
    }
  } else if (isError) {
    const h = humanize(error);
    // 503 from /readyz means "not ready", not "broken".
    if (h.code === "unavailable") {
      dot = "warn";
      label = "未就绪";
      title = "服务报告未就绪（/readyz 返回 503）";
    } else {
      dot = "bad";
      label = "不可达";
      title = h.title;
    }
  }

  return (
    <span
      title={title}
      className="inline-flex h-8 items-center gap-1.5 border border-rule-strong bg-panel px-2.5 text-xs text-ink-muted"
    >
      <span
        aria-hidden
        className={
          "h-2 w-2 rounded-full " +
          (dot === "ok"
            ? "bg-confirmed"
            : dot === "warn"
              ? "bg-warning"
              : dot === "bad"
                ? "bg-critical"
                : "bg-ink-faint animate-pulse")
        }
      />
      <span className="font-medium text-ink">{label}</span>
    </span>
  );
}
