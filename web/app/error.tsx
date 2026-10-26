"use client";

import { Button, Panel } from "@/components/ui/primitives";
import { describeRuntimeError } from "@/lib/hints";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  // ApiError 把 status/code 挂在实例上（见 lib/api/client.ts），但 Next 传到这里的
  // 类型是 Error，所以做一次结构化探测而不是 as any。
  const explain = describeRuntimeError({
    name: error.name,
    message: error.message,
    status: (error as { status?: number }).status,
    code: (error as { code?: string }).code,
  });
  return (
    <Panel title="页面出错" className="mx-auto mt-8 max-w-2xl">
      <div className="space-y-3 px-5 py-6">
        <p className="text-sm text-ink">{explain.title}</p>
        <p className="text-sm text-ink-faint">{explain.action}</p>
        <p className="reading break-all text-xs text-ink-faint">
          {error.message}
          {error.digest ? ` · digest ${error.digest}` : ""}
        </p>
        <Button variant="primary" onClick={reset}>
          重试
        </Button>
      </div>
    </Panel>
  );
}
