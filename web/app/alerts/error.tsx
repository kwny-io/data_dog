"use client";

import { Button, Panel } from "@/components/ui/primitives";
import { describeRuntimeError } from "@/lib/hints";

export default function AlertsError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  // 同全局错误页：先给可执行建议，机器串只作技术细节。
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
        </p>
        <Button variant="primary" onClick={reset}>
          重试
        </Button>
      </div>
    </Panel>
  );
}
