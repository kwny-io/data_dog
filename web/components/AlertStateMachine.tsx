"use client";

import clsx from "clsx";

import { TechValue } from "@/components/tech";
import { HScroll } from "@/components/ui/hscroll";
import {
  ALERT_STATE_LABEL,
  ALERT_STATE_STYLE,
  ALERT_TRANSITIONS,
  ACTIVE_ALERT_STATES,
  TONE_CLASS,
  type Tone,
} from "@/lib/domain";
import type { AlertState } from "@/lib/api/types";

/**
 * The alert lifecycle, drawn as the real state machine from the backend
 * (`alerting/lifecycle.py::TRANSITIONS`) — not as decorative pills.
 *
 * The spec calls this the biggest increment of the rewrite: the P3 loop has
 * never had a frontend before. So the structure is information:
 *  - states are laid out left→right by their position in the flow;
 *  - active states (detected/confirmed/escalated/recovering) are framed;
 *  - edges are drawn from the actual TRANSITIONS map, so the diagram cannot
 *    drift from the backend without the map changing;
 *  - each node shows its live count and is clickable to filter the list.
 */

// Column positions: 0..4 along the flow. Chosen to express the progression
// detected → confirmed → escalated → recovering → resolved → closed, with the
// two "parked" states (suppressed/rejected) hanging below detected.
const LAYOUT: Record<AlertState, { col: number; row: number }> = {
  detected: { col: 0, row: 0 },
  confirmed: { col: 1, row: 0 },
  escalated: { col: 2, row: 0 },
  recovering: { col: 3, row: 0 },
  resolved: { col: 4, row: 0 },
  closed: { col: 5, row: 0 },
  suppressed: { col: 0, row: 1 },
  rejected: { col: 1, row: 1 },
};

const EDGE_LABEL: Record<string, string> = {
  "detected→suppressed": "静默",
  "detected→rejected": "驳回",
  "resolved→detected": "复发",
  "suppressed→detected": "恶化",
  "rejected→detected": "重启",
  "confirmed→closed": "直接关闭",
};

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
  const nodes = Object.keys(LAYOUT) as AlertState[];
  const edges = collectEdges();

  return (
    <div className={className}>
      <HScroll>
        <div
          className="grid min-w-[720px] gap-x-6 gap-y-8 px-4 py-5"
          style={{ gridTemplateColumns: "repeat(6, minmax(90px, 1fr))" }}
        >
          {nodes.map((state) => {
            const pos = LAYOUT[state];
            const style = ALERT_STATE_STYLE[state];
            const count = counts?.[state] ?? 0;
            const isSel = selected === state;
            return (
              <div
                key={state}
                style={{ gridColumn: pos.col + 1, gridRow: pos.row + 1 }}
                className="flex flex-col items-center gap-1.5"
              >
                <StateNode
                  state={state}
                  tone={style.tone}
                  filled={count > 0}
                  count={count}
                  selected={isSel}
                  onClick={() => onSelect?.(isSel ? null : state)}
                />
                {/* show the transition verb on the outgoing edge for the mainline */}
                <EdgeCaption forState={state} />
              </div>
            );
          })}
        </div>
      </HScroll>

      {/* Legend sits OUTSIDE the scroller (it wraps rather than scrolls), so the
       *  scroll-edge fade never clips it. The map is data, so the legend states
       *  the invariants; the English enum names are demoted behind the raw-value
       *  toggle so it does not read as a parenthesised debug dump. */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-t border-rule px-4 py-3 text-[11px] text-ink-muted">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2.5 rounded-full bg-ink-faint" aria-hidden />
          实心＝当前有告警，空心＝无
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-3 w-4 border-2 border-rule-strong" aria-hidden />
          进行中：{ACTIVE_ALERT_STATES.map((s) => ALERT_STATE_LABEL[s]).join("、")}
          <TechValue value={ACTIVE_ALERT_STATES.join("/")} label="状态枚举" />
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2.5 border border-rule-strong" aria-hidden />
          终态无出边：{ALERT_STATE_LABEL.closed}
          <TechValue value="closed" label="状态枚举" />
        </span>
        <span className="flex items-center gap-1.5">
          <span className="reading" aria-hidden>
            {edges.length}
          </span>
          条合法流转，与后端流转表一致
          <TechValue value="TRANSITIONS" label="来源标识" />
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
    </div>
  );
}

function StateNode({
  state,
  tone,
  filled,
  count,
  selected,
  onClick,
}: {
  state: AlertState;
  tone: Tone;
  filled: boolean;
  count: number;
  selected: boolean;
  onClick: () => void;
}) {
  const style = ALERT_STATE_STYLE[state];
  const active = ACTIVE_ALERT_STATES.includes(state);
  const c = TONE_CLASS[tone];
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={selected}
      title={`${style.zh}（${state}）：当前 ${count} 条`}
      className={clsx(
        "group relative flex w-full flex-col items-center gap-1 border bg-panel px-2 py-2.5 transition-colors",
        c.border,
        selected && c.bg,
        active ? "border-2" : "border",
        "hover:bg-paper-sunken",
      )}
    >
      <span
        aria-hidden
        className={clsx("h-2.5 w-2.5 rounded-full", filled ? c.text : "border-2")}
        style={filled ? { backgroundColor: "currentColor" } : undefined}
      />
      <span className={clsx("text-xs font-semibold", c.text)}>{style.zh}</span>
      <TechValue value={state} label="状态枚举" />
      <span className={clsx("reading mt-0.5 text-sm font-semibold", count > 0 ? "text-ink" : "text-ink-faint")}>
        {count}
      </span>
    </button>
  );
}

function EdgeCaption({ forState }: { forState: AlertState }) {
  const captions: Partial<Record<AlertState, string>> = {
    detected: "→ 确认/抑制/驳回",
    confirmed: "→ 升级/恢复/关闭",
    escalated: "→ 恢复/关闭",
    recovering: "→ 解决/再升级",
    resolved: "→ 关闭/复发",
  };
  const txt = captions[forState];
  if (!txt) return <span className="h-4" aria-hidden />;
  return <span className="reading text-[10px] text-ink-faint">{txt}</span>;
}

function collectEdges(): string[] {
  const out: string[] = [];
  for (const [from, tos] of Object.entries(ALERT_TRANSITIONS)) {
    for (const to of tos) out.push(`${from}→${to}`);
  }
  return out;
}

export { EDGE_LABEL };
