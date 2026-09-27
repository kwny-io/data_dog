"use client";

import { Button, Panel } from "@/components/ui/primitives";

export default function AlertsError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <Panel title="告警页出错">
      <div className="space-y-3 px-4 py-5">
        <p className="text-sm text-ink">渲染告警页时遇到未预期的问题。</p>
        <p className="reading break-all text-xs text-ink-faint">{error.message}</p>
        <Button variant="primary" onClick={reset}>
          重试
        </Button>
      </div>
    </Panel>
  );
}
