"use client";

import clsx from "clsx";

import {
  Panel,
  SkeletonRows,
  EmptyState,
  ErrorState,
  Pagination,
  RowChevron,
} from "@/components/ui/primitives";
import { SeverityBadge } from "@/components/badges";
import { CopyId, TechValue } from "@/components/tech";
import { useCandidates } from "@/lib/api/queries";
import { emptyCandidatesHint } from "@/lib/hints";
import { useTenant } from "@/lib/providers";
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
  const { tenant } = useTenant();

  return (
    <Panel
      title="候选异常"
      subtitle={q.data ? `共 ${q.data.total} 条` : "加载中"}
      className="flex flex-col"
    >
      {q.isPending ? (
        <SkeletonRows rows={6} />
      ) : q.isError ? (
        <ErrorState
          {...humanize(q.error)}
          technical={technicalLine(q.error)}
          onRetry={() => q.refetch()}
        />
      ) : q.data.items.length === 0 ? (
        <EmptyState title="没有候选异常" hint={emptyCandidatesHint(tenant)} />
      ) : (
        <>
          <ul className="divide-rule divide-y">
            {q.data.items.map((c) => (
              // The row is selectable, but it also contains its own control
              // (CopyId renders a <button>). A <button> may not contain a
              // <button> — that is invalid HTML and React fails hydration on
              // it. So the select affordance is a full-bleed button *behind*
              // the content (z-0), and the content sits above it (z-10) with
              // pointer events off, re-enabled only on the copy control. Tab
              // lands on the row button; the copy button is its own tab stop.
              <li key={c.candidate_id} className="group/row relative">
                <button
                  type="button"
                  onClick={() => onSelect(c)}
                  aria-current={selectedId === c.candidate_id ? "true" : undefined}
                  aria-label={`查看候选 ${componentLabel(c.component_ref)} ${c.device_sn} 详情`}
                  className={clsx(
                    "row-tick group-hover/row:bg-paper-sunken group-hover/row:row-tick-active absolute inset-0 z-0 transition-all",
                    selectedId === c.candidate_id && "row-tick-active bg-primary-soft",
                  )}
                />
                <div className="pointer-events-none relative z-10 flex w-full items-center gap-2 px-3 py-2.5 text-left">
                  <span className="flex min-w-0 flex-1 flex-col gap-1">
                    <span className="flex items-center justify-between gap-2">
                      <span className="flex min-w-0 items-baseline gap-1.5">
                        <span className="text-ink truncate text-sm">
                          {componentLabel(c.component_ref)}
                        </span>
                        <TechValue value={c.component_ref} label="部件标识" />
                      </span>
                      <SeverityBadge severity={c.severity_hint} />
                    </span>
                    <span className="text-ink-faint flex items-center justify-between gap-2 text-[11px]">
                      <span className="flex min-w-0 items-baseline gap-1.5">
                        {/* 设备号必须在列表上。只有「左前髋关节电机 严重」的话，
                            机群里根本分不清是哪台机器出的问题——而这是值班人的第一个问题。 */}
                        <span className="truncate" title={c.device_sn}>
                          {c.device_sn}
                        </span>
                        <CopyId
                          id={c.candidate_id}
                          short={18}
                          className="pointer-events-auto"
                        />
                      </span>
                      <span
                        className={clsx(
                          "reading",
                          TONE_CLASS[severityTone(c.severity_hint)].text,
                        )}
                      >
                        评分 {c.anomaly_score.toFixed(2)}
                      </span>
                    </span>
                  </span>
                  <RowChevron className="affordance-chevron text-ink-faint group-hover/row:text-ink-muted group-hover/row:translate-x-0.5" />
                </div>
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
