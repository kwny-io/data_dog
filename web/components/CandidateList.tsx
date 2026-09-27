"use client";

import clsx from "clsx";

import { Panel, SkeletonRows, EmptyState, ErrorState, Pagination, RowChevron } from "@/components/ui/primitives";
import { SeverityBadge } from "@/components/badges";
import { CopyId, TechValue } from "@/components/tech";
import { useCandidates } from "@/lib/api/queries";
import { humanize, technicalLine } from "@/lib/errors";
import { componentLabel, severityTone, TONE_CLASS } from "@/lib/domain";
import type { CandidateEvent, PageParams } from "@/lib/api/types";

export function CandidateList({
  page,
  onPage,
  selectedId,
  onSelect,
  filters,
}: {
  page: PageParams;
  onPage: (p: PageParams) => void;
  selectedId: string | null;
  onSelect: (c: CandidateEvent) => void;
  filters: { device_sn?: string; component_ref?: string };
}) {
  const q = useCandidates(filters, page);

  return (
    <Panel
      title="候选异常"
      subtitle={q.data ? `共 ${q.data.total} 条` : "加载中"}
      className="flex flex-col"
      ticks
    >
      {q.isPending ? (
        <SkeletonRows rows={6} />
      ) : q.isError ? (
        <ErrorState {...humanize(q.error)} technical={technicalLine(q.error)} onRetry={() => q.refetch()} />
      ) : q.data.items.length === 0 ? (
        <EmptyState
          title="没有候选异常"
          hint={
            <>
              候选由在线数据面从原始日志产出。先向{" "}
              <code className="reading">POST /v1/telemetry</code> 提交一批设备日志，
              解析链会在这里生成候选。
            </>
          }
        />
      ) : (
        <>
          <ul className="divide-y divide-rule">
            {q.data.items.map((c) => (
              <li key={c.candidate_id}>
                <button
                  type="button"
                  onClick={() => onSelect(c)}
                  aria-current={selectedId === c.candidate_id ? "true" : undefined}
                  className={clsx(
                    // A selectable row carries a persistent left tick and a
                    // chevron so it reads as clickable without hover; the tint
                    // is the hover/selected enhancement, not the only cue.
                    "row-tick group/row flex w-full items-center gap-2 px-3 py-2.5 text-left transition-colors hover:bg-paper-sunken",
                    selectedId === c.candidate_id && "border-l-primary bg-primary-soft",
                  )}
                >
                  <span className="flex min-w-0 flex-1 flex-col gap-1">
                    <span className="flex items-center justify-between gap-2">
                      <span className="flex min-w-0 items-baseline gap-1.5">
                        <span className="truncate text-sm text-ink">
                          {componentLabel(c.component_ref)}
                        </span>
                        <TechValue value={c.component_ref} label="部件标识" />
                      </span>
                      <SeverityBadge severity={c.severity_hint} />
                    </span>
                    <span className="flex items-center justify-between gap-2 text-[11px] text-ink-faint">
                      <CopyId id={c.candidate_id} short={18} />
                      <span className={clsx("reading", TONE_CLASS[severityTone(c.severity_hint)].text)}>
                        评分 {c.anomaly_score.toFixed(2)}
                      </span>
                    </span>
                  </span>
                  <RowChevron className="affordance-chevron text-ink-faint group-hover/row:translate-x-0.5 group-hover/row:text-ink-muted" />
                </button>
              </li>
            ))}
          </ul>
          <Pagination
            count={q.data.count}
            total={q.data.total}
            limit={q.data.limit}
            offset={q.data.offset}
            hasMore={q.data.has_more}
            onChange={(offset) => onPage({ ...page, offset })}
            className="mt-auto"
          />
        </>
      )}
    </Panel>
  );
}
