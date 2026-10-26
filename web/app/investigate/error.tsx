"use client";

import { Button, Panel } from "@/components/ui/primitives";
import { describeRuntimeError } from "@/lib/hints";

export default function InvestigateError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  // 与全局错误页同一处理：能识别的运行时错误给出**可执行建议**，
  // 机器格式（`[429] rate_limited`）降级成技术细节，不占主位。
  const explain = describeRuntimeError({
    name: error.name,
    message: error.message,
    status: (error as { status?: number }).status,
    code: (error as { code?: string }).code,
    retryAfterSeconds: (error as { retryAfterSeconds?: number }).retryAfterSeconds,
  });
  return (
    <Panel title={explain.title}>
      <div className="space-y-3 px-4 py-5">
        <p className="text-ink text-sm">{explain.action}</p>
        <p className="reading text-ink-faint text-xs break-all">
          技术细节：{error.message}
          {error.digest ? ` · digest ${error.digest}` : ""}
        </p>
        <Button variant="primary" onClick={reset}>
          重试
        </Button>
      </div>
    </Panel>
  );
}
