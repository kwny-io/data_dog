"use client";

import clsx from "clsx";
import { useCallback, useEffect, useRef, useState } from "react";

import { useShowTech } from "@/lib/tech-fields";

/**
 * Raw-value primitives — the visual half of the demo/datum separation.
 *
 * The problem (see lib/vocabulary.ts): the wire format carries two vocabularies
 * at once — operator language ("右后膝关节电机") and machine language
 * ("joint_motor.rear_right_knee"). Printing both at the same visual rank is
 * what makes a screen read as a debug dump.
 *
 * The rule that fixes it: **a raw value never sits at the same rank as its
 * human label.** Two demotion levels, because the raw values are not all alike:
 *
 *   Level 1 — `CopyId`: always present, always demoted. Ids and timestamps are
 *     needed for tickets and log greps, so hiding them would cost the operator
 *     real work. They are small, faint, monospace, and carry a *persistent*
 *     copy affordance (not a hover reveal).
 *
 *   Level 2 — `TechValue`: gated behind the global "显示技术字段" switch
 *     (default off). This is for raw values that are pure redundancy — a field
 *     name the label already says, the English enum behind the Chinese state
 *     name, a snake_case ref beside its translation. Off by default so the
 *     screen reads clean; on demand for support and correlation.
 */

// ------------------------------------------------------------------ TechValue

export function TechValue({
  value,
  className,
  label,
}: {
  /** The raw machine value. Falsy values render nothing. */
  value?: string | null;
  className?: string;
  /** Accessible name; defaults to a generic one so the value is not orphaned. */
  label?: string;
}) {
  const showTech = useShowTech();
  if (!showTech || !value) return null;
  return (
    <span
      className={clsx("reading text-[10px] leading-tight text-ink-faint", className)}
      aria-label={label ? `${label}：${value}` : undefined}
    >
      {value}
    </span>
  );
}

/** A raw field name where a human label already carries the meaning.
 *  Renders the label, and the raw name only under the toggle. */
export function FieldName({ name, className }: { name: string; className?: string }) {
  const showTech = useShowTech();
  if (!showTech) return null;
  return (
    <code
      className={clsx(
        "reading rounded-[var(--radius-token)] border border-rule px-1 text-[10px] text-ink-faint",
        className,
      )}
    >
      {name}
    </code>
  );
}

/* --------------------------------------------------------------- CopyId *
 * An id chip in the secondary rank with an always-visible copy control.
 *
 * Why persistent and not hover-only: a hover reveal is invisible to keyboard
 * and touch users, and it makes everyone "guess wrong once" to learn the
 * affordance exists. The glyph is faint at rest and strengthens on hover —
 * hover enhances, it does not reveal. */

export function CopyId({
  id,
  short,
  className,
}: {
  id: string;
  /** Truncate to this many characters for display; the full id is copied. */
  short?: number;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(id);
    } catch {
      // Clipboard API is unavailable over plain HTTP on some hosts; fall back
      // to a hidden textarea + execCommand so the affordance never dead-ends.
      const ta = document.createElement("textarea");
      ta.value = id;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
      } catch {
        return; // give up quietly rather than throwing into the render path
      } finally {
        document.body.removeChild(ta);
      }
    }
    setCopied(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 1400);
  }, [id]);

  const display = short && id.length > short ? `${id.slice(0, short)}…` : id;

  return (
    <span className={clsx("group/id inline-flex items-center gap-1", className)}>
      <code className="reading text-[11px] text-ink-faint">{display}</code>
      <button
        type="button"
        onClick={copy}
        title={copied ? "已复制" : `复制 ${id}`}
        aria-label={copied ? "已复制到剪贴板" : `复制 ID ${id}`}
        className={clsx(
          "inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-[var(--radius-token)] border transition-colors",
          copied
            ? "border-confirmed text-confirmed"
            : "border-rule text-ink-faint hover:border-rule-strong hover:text-ink",
        )}
      >
        {copied ? <CheckGlyph /> : <CopyGlyph />}
      </button>
      <span aria-live="polite" className="sr-only">
        {copied ? "已复制" : ""}
      </span>
    </span>
  );
}

function CopyGlyph() {
  return (
    <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" aria-hidden>
      <rect x="9" y="9" width="12" height="12" />
      <path d="M5 15V5a2 2 0 0 1 2-2h10" />
    </svg>
  );
}

function CheckGlyph() {
  return (
    <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" aria-hidden>
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}
