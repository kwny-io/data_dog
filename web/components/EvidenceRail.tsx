"use client";

import clsx from "clsx";
import { useId, useState } from "react";

import { Badge } from "@/components/ui/primitives";
import { TechValue } from "@/components/tech";
import { formatValue, TONE_CLASS, type Tone } from "@/lib/domain";
import { componentName, metricName } from "@/lib/vocabulary";
import type { EvidenceSummary } from "@/lib/api/types";

/**
 * EvidenceRail — the signature device.
 *
 * A candidate's evidence is a `from_value → to_value` pair. That pair *is* a
 * measured interval, so it is drawn the way an engineering drawing states an
 * interval: a dimension line with extension lines at each end, arrowheads
 * pointing outward, and the value in a box. When a safe threshold is known
 * and the `to` value crosses it, the over-threshold span is hatched in the
 * severity colour — a tolerance band, which reads even in greyscale.
 *
 * This makes the backend's core principle ("a conclusion must carry its
 * evidence") visible and traceable. Expanding shows the source: the window,
 * the component, and any note/reference.
 *
 * Layout note: positions come from percentages inside a plain flex row, not
 * from an SVG viewBox — so the mono labels stay crisp and the rail reflows
 * without measuring the DOM.
 */

export interface EvidenceRailProps {
  evidence: EvidenceSummary;
  /** Safe limit, if known, e.g. 90 for a 90°C protection line. */
  threshold?: number | null;
  /** Component/window context shown when expanded. */
  context?: {
    componentRef?: string;
    windowStart?: string;
    windowEnd?: string;
  };
  tone?: Tone;
  className?: string;
}

function numeric(v: number | string | null | undefined): number | null {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function computeDomain(values: number[]): { lo: number; hi: number } {
  if (values.length === 0) return { lo: 0, hi: 1 };
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (lo === hi) {
    lo -= 1;
    hi += 1;
  }
  // If the range sits near zero, anchor at zero so growth reads proportionally.
  if (lo > 0 && lo < (hi - lo) * 0.75) lo = 0;
  const pad = (hi - lo) * 0.08;
  return { lo: lo - (lo === 0 ? 0 : pad), hi: hi + pad };
}

export function EvidenceRail({
  evidence,
  threshold = null,
  context,
  tone = "warning",
  className,
}: EvidenceRailProps) {
  const [open, setOpen] = useState(false);
  const panelId = useId();

  const from = numeric(evidence.from_value);
  const to = numeric(evidence.to_value);
  const baseline = numeric(evidence.baseline);
  const th = numeric(threshold);
  const unit = evidence.unit ?? "";

  const haveRail = from !== null && to !== null;
  const domainValues = [from, to, baseline, th].filter((v): v is number => v !== null);
  const { lo, hi } = computeDomain(domainValues);
  const pct = (v: number) => ((v - lo) / (hi - lo)) * 100;

  const crossed = th !== null && to !== null && to > th;
  const toneClass = TONE_CLASS[crossed ? "critical" : tone];
  const detailId = `${panelId}-detail`;

  return (
    <div className={clsx("group/rail", className)}>
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-xs text-ink-muted">
          {metricName(evidence.key)}
          <TechValue value={evidence.key} label="指标字段" className="ml-1.5" />
        </span>
        <span className="flex items-center gap-2">
          <span className={clsx("reading text-sm font-semibold", toneClass.text)}>
            {formatValue(evidence.from_value, unit)}
            <span aria-hidden className="mx-1 text-ink-faint">
              →
            </span>
            {formatValue(evidence.to_value, unit)}
          </span>
          {crossed && (
            <Badge tone="critical" filled>
              超阈值
            </Badge>
          )}
        </span>
      </div>

      {/* The rail itself */}
      <button
        type="button"
        aria-expanded={open}
        aria-controls={detailId}
        onClick={() => setOpen((v) => !v)}
        className="mt-2 block w-full cursor-pointer text-left"
        disabled={!haveRail}
      >
        <div className="relative h-9" aria-hidden={!haveRail}>
          {/* graph-paper guide across the whole span */}
          <div className="absolute left-0 right-0 top-1/2 h-px -translate-y-1/2 bg-rule" />

          {/* over-threshold hatch zone */}
          {crossed && th !== null && (
            <div
              className="hatch-critical absolute top-1/2 h-3 -translate-y-1/2 opacity-70"
              style={{ left: `${pct(th)}%`, width: `${Math.max(0, pct(to as number) - pct(th))}%` }}
            />
          )}

          {/* dimension line between from and to — a measured span, lit so it
           *  reads as a live instrument reading rather than a printed rule */}
          {haveRail && (
            <div
              className={clsx("absolute top-1/2 h-0.5 -translate-y-1/2 rounded-full", toneClass.bg)}
              style={{
                left: `${Math.min(pct(from as number), pct(to as number))}%`,
                width: `${Math.abs(pct(to as number) - pct(from as number))}%`,
                backgroundColor: crossed ? "var(--critical)" : undefined,
                boxShadow: crossed
                  ? "0 0 10px -1px var(--critical)"
                  : "0 0 10px -2px var(--glow-ring)",
              }}
            />
          )}

          {/* extension lines + value boxes at each end */}
          {haveRail && (
            <>
              <EndMark x={pct(from as number)} value={formatValue(evidence.from_value, unit)} align="left" />
              <EndMark x={pct(to as number)} value={formatValue(evidence.to_value, unit)} align="right" strong crossed={crossed} />
            </>
          )}

          {/* baseline tick (reference window) */}
          {baseline !== null && (
            <Tick x={pct(baseline)} label={`基准 ${formatValue(evidence.baseline, unit)}`} />
          )}

          {/* threshold tick (safe limit) */}
          {th !== null && <Tick x={pct(th)} label={`阈值 ${formatValue(threshold, unit)}`} danger />}
        </div>
      </button>

      {open && (
        <div id={detailId} className="mt-2 border-l-2 border-rule pl-3 text-xs text-ink-muted">
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
            {context?.componentRef && (
              <>
                <dt className="text-ink-faint">部件</dt>
                <dd className="break-all">
                  {componentName(context.componentRef)}
                  <TechValue value={context.componentRef} label="部件标识" className="ml-1.5" />
                </dd>
              </>
            )}
            {context?.windowStart && (
              <>
                <dt className="text-ink-faint">窗口</dt>
                <dd className="reading break-all">
                  {context.windowStart} → {context.windowEnd ?? "—"}
                </dd>
              </>
            )}
            {evidence.note && (
              <>
                <dt className="text-ink-faint">说明</dt>
                <dd>{evidence.note}</dd>
              </>
            )}
          </dl>
        </div>
      )}
    </div>
  );
}

function EndMark({
  x,
  value,
  align,
  strong = false,
  crossed = false,
}: {
  x: number;
  value: string;
  align: "left" | "right";
  strong?: boolean;
  crossed?: boolean;
}) {
  return (
    <div
      className="absolute top-0 flex h-full flex-col justify-between"
      style={{ left: `${x}%`, transform: "translateX(-50%)" }}
    >
      <span className={clsx("h-2 w-px", crossed ? "bg-critical" : "bg-rule-strong")} />
      <span
        className={clsx(
          "reading whitespace-nowrap border px-1 text-[10px] leading-tight",
          crossed ? "border-critical text-critical" : "border-rule-strong text-ink-muted",
        )}
        style={{ transform: align === "right" ? "translateX(-40%)" : "translateX(-60%)" }}
      >
        {strong ? <b className="font-semibold text-ink">{value}</b> : value}
      </span>
      <span className={clsx("h-2 w-px", crossed ? "bg-critical" : "bg-rule-strong")} />
    </div>
  );
}

function Tick({ x, label, danger = false }: { x: number; label: string; danger?: boolean }) {
  return (
    <div
      className="absolute top-0 flex h-full flex-col justify-between"
      style={{ left: `${x}%`, transform: "translateX(-50%)" }}
    >
      <span className={clsx("h-3 w-px border-l border-dashed", danger ? "border-critical" : "border-ink-faint")} />
      <span
        className={clsx(
          "reading whitespace-nowrap text-[10px]",
          danger ? "text-critical" : "text-ink-faint",
        )}
      >
        {label}
      </span>
      <span className={clsx("h-3 w-px border-l border-dashed", danger ? "border-critical" : "border-ink-faint")} />
    </div>
  );
}
