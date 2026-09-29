"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import clsx from "clsx";

/**
 * A horizontally scrollable region with a visible scroll affordance.
 *
 * Why this exists: mobile browsers hide scrollbars, so an `overflow-x-auto`
 * box whose content is wider than the screen gives NO signal that it scrolls.
 * To a touch user the content just looks clipped/broken (this is exactly how
 * the alert state machine read on a phone — see doc 38 §1.4). Desktop users
 * get a scrollbar; touch users get nothing. So we render an edge fade that
 * appears only when there is more content in that direction, and disappears
 * once you reach the end — a cue that is true at every scroll position.
 *
 * Lives in its own "use client" file rather than in primitives.tsx so that
 * server components (the evals page) can still import a *server* DataTable;
 * only this wrapper becomes a client island.
 */
export function HScroll({
  children,
  className,
  fadeClassName = "from-panel",
}: {
  children: React.ReactNode;
  className?: string;
  /** Tailwind `from-*` colour for the fade; must match the surface behind it. */
  fadeClassName?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ left: false, right: false });

  const update = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const left = el.scrollLeft > 2;
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 2;
    setEdges((prev) => (prev.left === left && prev.right === right ? prev : { left, right }));
  }, []);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    update();
    el.addEventListener("scroll", update, { passive: true });
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => {
      el.removeEventListener("scroll", update);
      ro.disconnect();
    };
  }, [update]);

  return (
    <div className={clsx("relative", className)}>
      <div ref={ref} className="overflow-x-auto">
        {children}
      </div>
      {edges.left && (
        <div
          aria-hidden
          className={clsx(
            "pointer-events-none absolute inset-y-0 left-0 w-8 bg-gradient-to-r to-transparent",
            fadeClassName,
          )}
        />
      )}
      {edges.right && (
        <div
          aria-hidden
          className={clsx(
            "pointer-events-none absolute inset-y-0 right-0 w-8 bg-gradient-to-l to-transparent",
            fadeClassName,
          )}
        />
      )}
    </div>
  );
}
