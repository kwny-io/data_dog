/**
 * Domain vocabulary: severity, alert lifecycle, dispositions.
 *
 * The brief (§6) pins four semantic colour roles — critical / warning /
 * confirmed / suppressed — and says consistency beats prettiness because the
 * alert state machine is read through them. Those four names are the tokens
 * (see globals.css). This file maps *states* onto those roles.
 *
 * One deliberate reading, flagged for review:
 *   The brief lists `confirmed` as the green role. "confirmed" is also the
 *   name of an alert lifecycle state (an active, human-acknowledged fault).
 *   Painting an active fault green would read as "fine" — the opposite of
 *   what an operator needs. So green (the `confirmed` token) is used for the
 *   *good outcome* of the lifecycle — `resolved` / `closed` — and an active
 *   confirmed fault is red. Token names still match the brief; only the
 *   state→role assignment is chosen for operational legibility.
 *   Change the single map below to switch this decision in one place.
 */

import type { AlertSeverity, AlertState, Disposition, SeverityHint } from "./api/types";

export type Tone = "critical" | "warning" | "confirmed" | "suppressed" | "primary";

/** Tailwind-safe class fragments per tone (kept literal so JIT sees them). */
export const TONE_CLASS: Record<Tone, { text: string; bg: string; border: string }> = {
  critical: {
    text: "text-critical",
    bg: "bg-critical-soft",
    border: "border-critical",
  },
  warning: {
    text: "text-warning",
    bg: "bg-warning-soft",
    border: "border-warning",
  },
  confirmed: {
    text: "text-confirmed",
    bg: "bg-confirmed-soft",
    border: "border-confirmed",
  },
  suppressed: {
    text: "text-suppressed",
    bg: "bg-suppressed-soft",
    border: "border-suppressed",
  },
  primary: {
    text: "text-primary",
    bg: "bg-primary-soft",
    border: "border-primary",
  },
};

// --------------------------------------------------------------- severity

export const SEVERITY_LABEL: Record<string, string> = {
  critical: "严重",
  warning: "警告",
  info: "提示",
};

export function severityTone(s: SeverityHint | AlertSeverity | string): Tone {
  switch (s) {
    case "critical":
      return "critical";
    case "warning":
      return "warning";
    case "info":
      return "suppressed";
    default:
      return "suppressed";
  }
}

// --------------------------------------------------------------- lifecycle

/** The backend's TRANSITIONS map (alerting/lifecycle.py). Used both to render
 *  the state machine and to validate a state's outgoing edges in the UI. */
export const ALERT_TRANSITIONS: Record<AlertState, AlertState[]> = {
  detected: ["confirmed", "suppressed", "rejected"],
  confirmed: ["escalated", "recovering", "closed"],
  escalated: ["recovering", "closed"],
  recovering: ["resolved", "escalated"],
  resolved: ["closed", "detected"],
  suppressed: ["detected"],
  rejected: ["detected"],
  closed: [],
};

/** States that count as "in progress" (lifecycle.ACTIVE_STATES). */
export const ACTIVE_ALERT_STATES: AlertState[] = [
  "detected",
  "confirmed",
  "escalated",
  "recovering",
];

/** Recommended reading order for the state-machine diagram (BFS from detected). */
export const ALERT_STATE_ORDER: AlertState[] = [
  "detected",
  "confirmed",
  "escalated",
  "recovering",
  "resolved",
  "closed",
  "suppressed",
  "rejected",
];

export const ALERT_STATE_LABEL: Record<AlertState, string> = {
  detected: "已检测",
  confirmed: "已确认",
  escalated: "已升级",
  recovering: "恢复中",
  resolved: "已解决",
  closed: "已关闭",
  suppressed: "已抑制",
  rejected: "已驳回",
};

export interface AlertStateStyle {
  tone: Tone;
  /** Filled dot vs hollow: distinguishes states that share a tone. */
  filled: boolean;
  zh: string;
}

export const ALERT_STATE_STYLE: Record<AlertState, AlertStateStyle> = {
  detected: { tone: "warning", filled: false, zh: "已检测" },
  confirmed: { tone: "critical", filled: true, zh: "已确认" },
  escalated: { tone: "critical", filled: true, zh: "已升级" },
  recovering: { tone: "primary", filled: false, zh: "恢复中" },
  resolved: { tone: "confirmed", filled: true, zh: "已解决" },
  closed: { tone: "suppressed", filled: true, zh: "已关闭" },
  suppressed: { tone: "suppressed", filled: false, zh: "已抑制" },
  rejected: { tone: "suppressed", filled: true, zh: "已驳回" },
};

export function isActiveAlert(state: AlertState): boolean {
  return ACTIVE_ALERT_STATES.includes(state);
}

// ------------------------------------------------------------- disposition

export const DISPOSITION_LABEL: Record<Disposition, string> = {
  confirmed: "确认为故障",
  rejected: "排除故障",
  inconclusive: "证据不足",
  needs_human_review: "需要人工复核",
};

export function dispositionTone(d: Disposition | string): Tone {
  switch (d) {
    case "confirmed":
      return "critical";
    case "rejected":
      return "suppressed";
    case "inconclusive":
      return "warning";
    case "needs_human_review":
      return "warning";
    default:
      return "suppressed";
  }
}

export const HYPOTHESIS_STATUS_LABEL: Record<string, string> = {
  supported: "有支持证据",
  unsupported: "有反证",
  unverified: "待验证",
};

// ------------------------------------------------------------------- misc

/**
 * Friendly component label: "joint_motor.front_left_hip" → "左前髋关节电机".
 *
 * Delegates to lib/vocabulary.ts, which composes an arbitrary ref token by
 * token instead of consulting a closed lookup table — so an unlisted joint
 * still degrades to readable Chinese rather than dumping "joint_motor.rear...
 * back at the operator. The raw ref is available via <TechValue/>.
 */
export { componentName as componentLabel } from "./vocabulary";

/** Format a metric value with its unit, without inventing precision. */
export function formatValue(v: number | string | null | undefined, unit?: string | null): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") {
    const s = Number.isInteger(v) ? String(v) : v.toFixed(2).replace(/\.?0+$/, "");
    return unit ? `${s}${unit}` : s;
  }
  return unit ? `${v}${unit}` : String(v);
}

/** Absolute local time from an ISO string; falls back to the raw value. */
export function formatTime(iso: string | number | null | undefined): string {
  if (iso === null || iso === undefined) return "—";
  const d = typeof iso === "number" ? new Date(iso * (iso < 1e12 ? 1000 : 1)) : new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString("zh-CN", { hour12: false });
}

export function shortId(id: string, head = 12): string {
  return id.length > head ? `${id.slice(0, head)}…` : id;
}
