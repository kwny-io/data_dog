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

export function SeverityBadge({ severity }: { severity: SeverityHint | AlertSeverity | string }) {
  return (
    <Badge tone={severityTone(severity)} dot filled>
      {SEVERITY_LABEL[severity] ?? severity}
      <TechValue value={severity} label="严重度枚举" className="ml-1" />
    </Badge>
  );
}

export function AlertStateBadge({ state }: { state: AlertState }) {
  const s = ALERT_STATE_STYLE[state];
  return (
    <Badge tone={s.tone} dot filled={s.filled}>
      {s.zh}
      <TechValue value={state} label="状态枚举" className="ml-1" />
    </Badge>
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
