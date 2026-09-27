"use client";

import { useState } from "react";
import clsx from "clsx";

import { TechValue } from "@/components/tech";
import { TONE_CLASS } from "@/lib/domain";
import type { TraceStep } from "@/lib/api/types";

/**
 * The ReAct trajectory as a real numbered sequence.
 *
 * The spec's structural principle: the trace IS a sequence, so it gets real
 * step numbers — numbering is not decoration here. Each step is a tool call
 * with its arguments and the (evidence) result; the result is what makes the
 * conclusion auditable, so it is shown, not hidden behind a hover.
 */

const TOOL_META: Record<string, { zh: string; glyph: string }> = {
  query_candidate_evidence: { zh: "查候选证据", glyph: "◉" },
  query_vendor_knowledge: { zh: "查厂商知识", glyph: "▤" },
  query_neighbor_events: { zh: "查邻居事件", glyph: "◫" },
  query_recent_changes: { zh: "查近期变更", glyph: "◈" },
};

export function TraceTimeline({ trace }: { trace: TraceStep[] }) {
  if (trace.length === 0) {
    return (
      <p className="px-4 py-6 text-sm text-ink-muted">
        没有轨迹。若刚提交调查，轨迹会随结果一起返回。
      </p>
    );
  }
  return (
    <ol className="divide-y divide-rule">
      {trace.map((step) => (
        <TraceRow key={`${step.step}-${step.name}`} step={step} />
      ))}
    </ol>
  );
}

function TraceRow({ step }: { step: TraceStep }) {
  const [open, setOpen] = useState(true);
  const meta = TOOL_META[step.name] ?? { zh: step.name, glyph: "◆" };

  return (
    <li className="px-4 py-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-3 text-left"
      >
        <span
          className="reading flex h-6 w-6 shrink-0 items-center justify-center border border-rule-strong bg-paper-sunken text-xs font-semibold text-ink"
          aria-hidden
        >
          {step.step}
        </span>
        <span className={clsx("text-sm", TONE_CLASS.primary.text)} aria-hidden>
          {meta.glyph}
        </span>
        <span className="text-sm font-medium text-ink">{meta.zh}</span>
        <TechValue value={step.name} label="工具名" className="truncate" />
        <span className="ml-auto text-[11px] text-ink-faint">{open ? "收起" : "展开"}</span>
      </button>

      {open && (
        <div className="mt-2 space-y-2 pl-9">
          <KeyVals label="调用参数" value={step.args} />
          <KeyVals label="返回结果" value={step.result} emphasize />
        </div>
      )}
    </li>
  );
}

function KeyVals({
  label,
  value,
  emphasize = false,
}: {
  label: string;
  value: Record<string, unknown>;
  emphasize?: boolean;
}) {
  const entries = flatten(value);
  if (entries.length === 0) {
    return (
      <div className="text-xs text-ink-faint">
        {label}：（空）
      </div>
    );
  }
  return (
    <div>
      <div className="mb-1 text-[11px] text-ink-faint">{label}</div>
      <dl
        className={clsx(
          "grid grid-cols-[minmax(7rem,auto)_1fr] gap-x-3 gap-y-0.5 border-l-2 pl-3",
          emphasize ? "border-primary" : "border-rule",
        )}
      >
        {entries.map(([k, v]) => (
          <FragmentRow key={k} k={k} v={v} />
        ))}
      </dl>
    </div>
  );
}

function FragmentRow({ k, v }: { k: string; v: string }) {
  return (
    <>
      <dt className="reading truncate text-[11px] text-ink-faint">{k}</dt>
      <dd className="reading break-all text-xs text-ink">{v}</dd>
    </>
  );
}

/** Flatten one level of nesting into "a.b" — keeps long evidence JSON readable. */
function flatten(obj: Record<string, unknown>, prefix = ""): Array<[string, string]> {
  const out: Array<[string, string]> = [];
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v !== null && typeof v === "object" && !Array.isArray(v)) {
      out.push(...flatten(v as Record<string, unknown>, key));
    } else if (Array.isArray(v)) {
      out.push([key, v.map((x) => (typeof x === "object" ? JSON.stringify(x) : String(x))).join(", ")]);
    } else {
      out.push([key, v === null || v === undefined ? "—" : String(v)]);
    }
  }
  return out;
}
