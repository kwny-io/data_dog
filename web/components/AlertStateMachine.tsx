"use client";

import clsx from "clsx";

import { HScroll } from "@/components/ui/hscroll";
import {
  ALERT_STATE_LABEL,
  ALERT_STATE_STYLE,
  ACTIVE_ALERT_STATES,
  TONE_CLASS,
} from "@/lib/domain";
import type { AlertState } from "@/lib/api/types";

/**
 * The alert lifecycle, drawn as the backend's real state machine.
 *
 * Read it as a signal path, not a row of pills. The six forward states are
 * stops on a single spectrum rail — the gradient runs cyan→violet→magenta, so
 * the direction of travel is legible without arrows — and each stop carries its
 * live count, because this is a distribution view: where are the alerts right
 * now. The two bypass states (suppressed / rejected) hang below the rail; they
 * are side-branches off "detected", not stops on the mainline.
 *
 * Every legal transition is enumerated beneath the rail, so the picture is a
 * complete account of the backend TRANSITIONS map without a routing diagram
 * that would collapse at narrow widths. Each stop is a filter control.
 */

const MAINLINE: AlertState[] = [
  "detected",
  "confirmed",
  "escalated",
  "recovering",
  "resolved",
  "closed",
];

const SIDE_STATES: AlertState[] = ["suppressed", "rejected"];

/** Transitions that leave the drawn mainline: the branch and back edges. */
const OFF_RAIL: { from: AlertState; verb: string; to: AlertState }[] = [
  { from: "detected", verb: "静默", to: "suppressed" },
  { from: "detected", verb: "驳回", to: "rejected" },
  { from: "confirmed", verb: "直接关闭", to: "closed" },
  { from: "escalated", verb: "关闭", to: "closed" },
  { from: "recovering", verb: "再升级", to: "escalated" },
  { from: "resolved", verb: "复发", to: "detected" },
  { from: "suppressed", verb: "恶化", to: "detected" },
  { from: "rejected", verb: "重启", to: "detected" },
];

export function AlertStateMachine({
  counts,
  selected,
  onSelect,
  className,
}: {
  counts?: Partial<Record<AlertState, number>>;
  selected?: AlertState | null;
  onSelect?: (state: AlertState | null) => void;
  className?: string;
}) {
  const countOf = (s: AlertState) => counts?.[s] ?? 0;
  const interactive = !!onSelect;

  return (
    <div className={className}>
      <HScroll>
        <div className="min-w-[640px] px-4 pt-5 pb-4">
          {/* The rail: six stops along one spectrum hairline. */}
          <div className="relative flex h-20 items-center justify-between gap-1">
            <span
              aria-hidden
              className="pointer-events-none absolute inset-x-2 top-1/2 h-px -translate-y-1/2 bg-gradient-to-r from-spec-cyan/35 via-spec-violet/40 to-spec-magenta/35"
            />
            {MAINLINE.map((state) => (
              <StateStop
                key={state}
                state={state}
                count={countOf(state)}
                selected={selected === state}
                interactive={interactive}
                onClick={() => onSelect?.(selected === state ? null : state)}
              />
            ))}
          </div>

          {/* Bypass states — side-branches off "detected". */}
          <div className="relative mt-4 flex flex-wrap items-center gap-x-3 gap-y-2 pl-6">
            <span
              aria-hidden
              className="absolute -top-4 left-[27px] h-4 w-px border-l border-dashed border-rule-strong"
            />
            <span className="text-[11px] text-ink-faint">旁路</span>
            {SIDE_STATES.map((state) => (
              <StateStop
                key={state}
                state={state}
                count={countOf(state)}
                selected={selected === state}
                interactive={interactive}
                compact
                onClick={() => onSelect?.(selected === state ? null : state)}
              />
            ))}
            <span className="text-[11px] text-ink-faint">
              检测后被静默 / 驳回的候选止步于此，仍可回到已检测
            </span>
          </div>
        </div>
      </HScroll>

      {/* Legend, then the full transition list. */}
      <div className="space-y-2 border-t border-rule px-4 py-3 text-[11px] text-ink-muted">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
          <span className="flex items-center gap-1.5">
            <span aria-hidden className="inline-block h-2.5 w-2.5 rounded-full bg-ink-faint" />
            实心＝当前有该状态的告警，空心＝无
          </span>
          <span className="flex items-center gap-1.5">
            <span aria-hidden className="inline-block h-3 w-4 border-2 border-rule-strong" />
            进行中：{ACTIVE_ALERT_STATES.map((s) => ALERT_STATE_LABEL[s]).join("、")}
          </span>
          <span className="flex items-center gap-1.5">
            <span aria-hidden className="inline-block h-2.5 w-2.5 border border-rule-strong" />
            {ALERT_STATE_LABEL.closed}为终态，无出边
          </span>
          {onSelect && (
            <button
              type="button"
              onClick={() => onSelect(null)}
              className="ml-auto text-primary hover:underline"
            >
              清除筛选
            </button>
          )}
        </div>
        <p className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-ink-faint">
          <span>合法流转：</span>
          <span className="whitespace-nowrap">
            已检测→确认→已确认→升级→已升级→恢复→恢复中→解决→已解决→关闭→已关闭
          </span>
          {OFF_RAIL.map((e, i) => (
            <span key={i} className="whitespace-nowrap">
              {ALERT_STATE_LABEL[e.from]}→{e.verb}→{ALERT_STATE_LABEL[e.to]}
            </span>
          ))}
        </p>
      </div>
    </div>
  );
}

function StateStop({
  state,
  count,
  selected,
  interactive,
  compact = false,
  onClick,
}: {
  state: AlertState;
  count: number;
  selected: boolean;
  interactive: boolean;
  /** The bypass-chip shape, for the states that hang off the rail. */
  compact?: boolean;
  onClick: () => void;
}) {
  const style = ALERT_STATE_STYLE[state];
  const c = TONE_CLASS[style.tone];
  const lit = count > 0;

  if (compact) {
    return (
      <button
        type="button"
        onClick={onClick}
        aria-pressed={selected}
        title={`${style.zh}：当前 ${count} 条`}
        className={clsx(
          "group inline-flex items-center gap-2 rounded-full border px-2.5 py-1 transition-colors",
          selected
            ? clsx(c.border, c.bg, c.text)
            : lit
              ? clsx(c.border, "text-ink")
              : "border-rule text-ink-muted",
          interactive ? "cursor-pointer hover:border-rule-strong" : "cursor-default",
        )}
      >
        <span
          aria-hidden
          data-dot
          className={clsx(
            "h-2 w-2 rounded-full",
            lit ? clsx(c.text, "bg-current") : "border border-rule-strong",
          )}
        />
        <span className="text-xs font-medium">{style.zh}</span>
        <span className={clsx("reading text-xs font-semibold", lit ? "text-ink" : "text-ink-faint")}>
          {count}
        </span>
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={selected}
      title={`${style.zh}：当前 ${count} 条`}
      className={clsx(
        "relative z-[1] flex h-full flex-1 flex-col items-center justify-center rounded-[var(--radius-token)]",
        interactive ? "cursor-pointer" : "cursor-default",
      )}
    >
      <span
        className={clsx(
          "flex h-6 items-center whitespace-nowrap rounded-full border px-2.5 text-xs font-semibold transition-all",
          c.border,
          c.text,
          selected || lit
            ? clsx(c.bg, "shadow-[0_0_12px_-3px_currentColor]")
            : "bg-panel/60",
          interactive && "group-hover:brightness-110",
        )}
      >
        {style.zh}
      </span>
      <span aria-hidden className="relative my-2 flex h-4 w-4 items-center justify-center">
        <span
          data-dot
          className={clsx(
            "h-3 w-3 rounded-full transition-all",
            lit
              ? clsx(c.text, "border-2 border-current bg-current")
              : "border-2 border-rule-strong bg-panel",
          )}
          style={lit ? { boxShadow: "0 0 10px -1px currentColor" } : undefined}
        />
        {selected && <span className={clsx("absolute inset-[-3px] rounded-full border", c.border)} />}
      </span>
      <span
        className={clsx(
          "reading flex h-6 items-center text-base font-semibold",
          lit ? "text-ink" : "text-ink-faint",
        )}
      >
        {count}
      </span>
    </button>
  );
}
