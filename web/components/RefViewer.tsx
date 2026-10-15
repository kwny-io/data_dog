"use client";

import { useState } from "react";

import { Badge, ErrorState, Panel, SkeletonRows } from "@/components/ui/primitives";
import { useCandidateRefs } from "@/lib/api/queries";
import { humanize, technicalLine } from "@/lib/errors";
import type { ResolvedRef } from "@/lib/api/types";

/**
 * EvidenceRefsPanel — turn a candidate's opaque evidence refs into readable
 * evidence ("what did this conclusion actually point at?").
 *
 * Two deliberate properties:
 *
 *  1. **Lazy**: nothing is requested until the operator presses the button.
 *     Resolving `rawlog://` reads the raw log body, which is **audited**
 *     server-side (`action=rawlog.read`) and can be large — doing it on render
 *     would spam the audit trail and pull payloads nobody asked for.
 *  2. **Honest**: a failed ref shows the backend's finite `error` code, and a
 *     truncated body says so (with its byte count) instead of looking complete.
 */
export function EvidenceRefsPanel({
  candidateId,
  refs,
}: {
  candidateId: string;
  refs: string[];
}) {
  const [requested, setRequested] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const q = useCandidateRefs(candidateId, { enabled: requested });
  const items: ResolvedRef[] = q.data?.results ?? [];

  return (
    <Panel
      title="证据引用"
      subtitle={refs.length + " 条 · 解析后可见出处正文"}
      actions={
        <button
          type="button"
          className="rounded border border-rule px-2 py-1 text-xs text-ink-muted hover:text-ink disabled:opacity-50"
          onClick={() => setRequested(true)}
          disabled={q.isFetching}
        >
          {q.isFetching ? "解析中…" : requested ? "重新解析" : "解析证据引用"}
        </button>
      }
    >
      {!requested && (
        <p className="px-4 py-4 text-sm text-ink-muted">
          尚未解析。点「解析证据引用」才会读取——原始日志正文的读取会**留审计痕迹**。
        </p>
      )}
      {requested && q.isPending && <SkeletonRows rows={3} />}
      {requested && q.isError && (
        <ErrorState
          {...humanize(q.error)}
          technical={technicalLine(q.error)}
          onRetry={() => q.refetch()}
        />
      )}
      {requested && q.isSuccess && items.length === 0 && (
        <p className="px-4 py-4 text-sm text-ink-muted">该候选没有可解析的证据引用。</p>
      )}
      {requested && q.isSuccess && items.length > 0 && (
        <ul className="divide-y divide-rule">
          {items.map((item, i) => {
            const key = item.ref?.authority ?? String(i);
            const expanded = open === key;
            return (
              <li key={key + "-" + String(i)} className="px-4 py-3">
                <button
                  type="button"
                  className="flex w-full items-center gap-2 text-left"
                  aria-expanded={expanded}
                  onClick={() => setOpen(expanded ? null : key)}
                >
                  <Badge tone={item.ok ? "primary" : "critical"}>
                    {item.ref?.scheme ?? "?"}
                  </Badge>
                  <span className="truncate font-mono text-xs text-ink-muted">{key}</span>
                  <span className="ml-auto text-xs text-ink-faint">
                    {item.ok ? "已解析" : item.error}
                  </span>
                </button>
                {expanded && <RefBody item={item} />}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

function RefBody({ item }: { item: ResolvedRef }) {
  if (!item.ok) {
    return (
      <p className="mt-2 text-xs text-ink-muted">
        解析失败：{item.error}
        {item.detail ? " — " + item.detail : ""}
      </p>
    );
  }
  const p = item.payload ?? {};
  const content = typeof p.content === "string" ? p.content : null;
  return (
    <div className="mt-2 space-y-2" data-testid="ref-body">
      <pre className="max-h-64 overflow-auto rounded bg-surface-sunken p-2 text-xs text-ink">
        {content !== null ? content : JSON.stringify(p, null, 2)}
      </pre>
      <div className="flex flex-wrap gap-3 text-xs text-ink-muted">
        {p.bytes !== undefined && <span>字节 {String(p.bytes)}</span>}
        {p.truncated === true && <span data-testid="ref-truncated">已截断</span>}
        {typeof p.content_sha256 === "string" && (
          <span className="font-mono">sha256 {String(p.content_sha256).slice(0, 16)}…</span>
        )}
      </div>
      {typeof p.note === "string" && <p className="text-xs text-ink-faint">{p.note}</p>}
    </div>
  );
}
