"use client";

import { Button, Panel } from "@/components/ui/primitives";

export default function InvestigateError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <Panel title="调查台出错">
      <div className="space-y-3 px-4 py-5">
        <p className="text-sm text-ink">
          这个页面在渲染时遇到未预期的问题。已选候选不会丢失，可以重试。
        </p>
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
