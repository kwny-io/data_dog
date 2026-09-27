"use client";

import { useCallback, useSyncExternalStore } from "react";

/**
 * Global visibility of raw machine values — `event_type`, wire ids, field
 * names, snake_case component refs.
 *
 * Default OFF. The primary read is human language (see lib/vocabulary.ts);
 * a raw identifier is a support/correlation affordance, not a headline.
 * An operator pasting an id into a ticket turns this on; everyone else never
 * sees snake_case. See the "two vocabularies" note in vocabulary.ts.
 *
 * Implemented as an external store + `useSyncExternalStore` rather than a
 * React context with a `useState` initializer:
 *  - `getServerSnapshot` returns the default (off), so the server HTML and the
 *    client's first render agree — no hydration mismatch when the stored value
 *    is "on";
 *  - the `storage` listener means toggling the switch in one tab is reflected
 *    in every other open tab, which context state cannot do.
 */

const KEY = "dd-show-tech";

let current: boolean | null = null;
const listeners = new Set<() => void>();

function read(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange);
  // `storage` fires on other tabs/windows; sync so the switch never disagrees.
  const onStorage = (e: StorageEvent) => {
    if (e.key === KEY || e.key === null) {
      current = read();
      onChange();
    }
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(onChange);
    window.removeEventListener("storage", onStorage);
  };
}

function getSnapshot(): boolean {
  current ??= read();
  return current;
}

function getServerSnapshot(): boolean {
  return false;
}

/** Write the flag and notify every subscriber (this tab's + other tabs'). */
export function setShowTech(v: boolean): void {
  current = v;
  try {
    window.localStorage.setItem(KEY, v ? "1" : "0");
  } catch {
    /* storage may be unavailable; the choice still applies for the session */
  }
  for (const l of listeners) l();
}

export function useShowTech(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}

/** Drop the module-level snapshot cache. Test-only: keeps one test's toggle
 *  from leaking into the next (the module is not re-imported between tests). */
export function resetTechFields(): void {
  current = null;
}

export function useTechFields(): {
  showTech: boolean;
  setShowTech: (v: boolean) => void;
  toggleTech: () => void;
} {
  const showTech = useShowTech();
  const toggleTech = useCallback(() => setShowTech(!showTech), [showTech]);
  return { showTech, setShowTech, toggleTech };
}

/** Inline script: mark the document when the flag is on, before first paint,
 *  so demoted/secondary styling is correct without a flash. */
export const techInitScript = `(function(){try{if(localStorage.getItem("${KEY}")==="1"){document.documentElement.dataset.showTech="1";}}catch(e){}})();`;
