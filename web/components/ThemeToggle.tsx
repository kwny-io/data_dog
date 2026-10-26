"use client";

import { useEffect, useState } from "react";

import { useTheme } from "@/lib/providers";

/** Cycles light → dark → system. The current mode is named in the title so
 *  the control is understandable without guessing from an icon.
 *
 *  The stored theme lives in localStorage, which the server cannot read — so
 *  during SSR (and the first client render, which must match it) the theme is
 *  unknown. Rendering the real icon there would make SSR and the client
 *  disagree on the SVG tree and fail hydration. So we hold a neutral
 *  placeholder until mounted, then show the true icon. The button keeps its
 *  exact box, so nothing shifts. */
export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const order = ["light", "dark", "system"] as const;
  const next = order[(order.indexOf(theme) + 1) % order.length] ?? "system";
  const label = surface(theme);

  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      title={mounted ? `主题：${label}（点击切换到「${surface(next)}」）` : "切换主题"}
      aria-label={mounted ? `主题：${label}。点击切换到${surface(next)}` : "切换主题"}
      className="inline-flex h-8 w-8 items-center justify-center rounded-full border border-rule-strong bg-panel/50 text-ink-muted transition-colors hover:border-spec-violet/60 hover:text-ink"
    >
      {!mounted ? (
        <span aria-hidden className="h-[15px] w-[15px]" />
      ) : theme === "light" ? (
        <SunIcon />
      ) : theme === "dark" ? (
        <MoonIcon />
      ) : (
        <AutoIcon />
      )}
    </button>
  );
}

function surface(t: "light" | "dark" | "system"): string {
  return t === "light" ? "浅色" : t === "dark" ? "深色" : "跟随系统";
}

function SunIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  );
}
function MoonIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
    </svg>
  );
}
function AutoIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
      <rect x="3" y="4" width="18" height="12" rx="1" />
      <path d="M8 20h8M12 16v4" />
    </svg>
  );
}
