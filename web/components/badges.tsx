"use client";

import { Badge } from "@/components/ui/primitives";
import { TechValue } from "@/components/tech";
import {
  ALERT_STATE_STYLE,
  DISPOSITION_LABEL,
  HYPOTHESIS_STATUS_LABEL,
  SEVERITY_LABEL,
  TONE_CLASS,
  dispositionTone,
  severityTone,
  type Tone,
} from "@/lib/domain";
import type { AlertSeverity, AlertState, Disposition, SeverityHint } from "@/lib/api/types";
import clsx from "clsx";

/**
 * StateBox — the state/severity marker, drawn in the same language as the
 * state-machine node: a squared, tone-bordered plate rather than a pill. The
 * box is the console's "this is a classified value" shape, so a state reads the
 * same whether it sits on the machine or in a table row.
 */
function StateBox({
  tone,
  children,
  tech,
  techLabel,
}: {
  tone: Tone;
  children: React.ReactNode;
  tech?: string;
  techLabel?: string;
}) {
  const c = TONE_CLASS[tone];
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 rounded-[var(--radius-token)] border px-2 py-0.5 text-xs font-semibold",
        c.border,
        c.text,
      )}
    >
      {children}
      {tech && <TechValue value={tech} label={techLabel} />}
    </span>
  );
}

export function SeverityBadge({ severity }: { severity: SeverityHint | AlertSeverity | string }) {
  return (
    <StateBox tone={severityTone(severity)} tech={severity} techLabel="严重度枚举">
      {SEVERITY_LABEL[severity] ?? severity}
    </StateBox>
  );
}

export function AlertStateBadge({ state }: { state: AlertState }) {
  const s = ALERT_STATE_STYLE[state];
  return (
    <StateBox tone={s.tone} tech={state} techLabel="状态枚举">
      {s.zh}
    </StateBox>
  );
}

export function DispositionBadge({ disposition }: { disposition: Disposition | string }) {
  return (
    <Badge tone={dispositionTone(disposition)}>
      {DISPOSITION_LABEL[disposition as Disposition] ?? disposition}
    </Badge>
  );
}

export function HypothesisStatusBadge({ status }: { status: string }) {
  const tone: Tone =
    status === "supported" ? "confirmed" : status === "unsupported" ? "critical" : "warning";
  return (
    <span className={clsx("text-xs font-medium", TONE_CLASS[tone].text)}>
      {HYPOTHESIS_STATUS_LABEL[status] ?? status}
    </span>
  );
}
