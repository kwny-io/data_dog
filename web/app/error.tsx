"use client";

import { Button, Panel } from "@/components/ui/primitives";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <Panel title="页面出错" className="mx-auto mt-8 max-w-2xl">
      <div className="space-y-3 px-5 py-6">
        <p className="text-sm text-ink">这个页面遇到了未预期的问题，其余功能不受影响。</p>
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
