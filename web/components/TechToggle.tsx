"use client";

import clsx from "clsx";

import { useTechFields } from "@/lib/tech-fields";

/**
 * The global switch behind the "two vocabularies" fix (see components/tech.tsx).
 *
 * Off (default): raw machine values — field names, English enums, snake_case
 * refs — are hidden, and the screen reads in one language.
 * On: those values reappear in the secondary rank, for correlating against
 * logs or pasting into a ticket.
 *
 * A labelled, always-visible control rather than an icon: a hidden diagnostic
 * toggle nobody can find is worse than no toggle. The state is named in the
 * text ("技术字段" with an on/off word), not encoded in colour alone.
 */
export function TechToggle() {
  const { showTech, toggleTech } = useTechFields();

  return (
    <button
      type="button"
      onClick={toggleTech}
      aria-pressed={showTech}
      title={
        showTech
          ? "技术字段：显示中。点击隐藏原始字段名与英文枚举"
          : "技术字段：已隐藏。点击显示原始字段名、英文枚举与 snake_case 标识"
      }
      className={clsx(
        "shine inline-flex h-8 items-center gap-1.5 rounded-full border px-2.5 text-xs transition-colors",
        showTech
          ? "border-spec-violet/70 bg-primary-soft font-medium text-primary shadow-[0_0_16px_-6px_var(--glow-ring)]"
          : "border-rule-strong bg-panel/50 text-ink-muted hover:border-spec-violet/60 hover:text-ink",
      )}
    >
      {/* A bracket pair reads as "raw / bracketed datum" — the same visual
       *  language as the mono ids, without an ambiguous gear icon. */}
      <span aria-hidden className="reading text-[11px]">
        {"{}"}
      </span>
      <span>技术字段</span>
    </button>
  );
}
