"use client";

import { useEffect, useRef } from "react";

import clsx from "clsx";

/**
 * A pointer-tracked spotlight over a surface.
 *
 * Why this is a client component and not pure CSS: the highlight must follow
 * the cursor, which CSS cannot do on its own. The important part is HOW it
 * updates — on pointermove we write `--spot-x` / `--spot-y` straight to the
 * node's style and flip a `data-lit` attribute, all WITHOUT React state. No
 * re-render, no new markup, so this decoration can never become a hydration
 * mismatch: server and first client render are identical (both unlit), and the
 * effect only ever mutates attributes after mount.
 *
 * On touch there is no hover, so it simply never lights — nothing is lost,
 * because the spotlight carries no information; the panel's own border and the
 * row affordances carry the meaning.
 */
export function Spotlight({
  children,
  className,
  radius = 240,
  as: Tag = "div",
}: {
  children: React.ReactNode;
  className?: string;
  /** Spotlight radius in px; sets the falloff size of the gradient. */
  radius?: number;
  as?: "div" | "section" | "article";
}) {
  const ref = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    // Coarse pointers (touch) never hover; skip the listener entirely.
    if (!window.matchMedia("(hover: hover)").matches) return;

    const onMove = (e: PointerEvent) => {
      const r = el.getBoundingClientRect();
      el.style.setProperty("--spot-x", `${e.clientX - r.left}px`);
      el.style.setProperty("--spot-y", `${e.clientY - r.top}px`);
    };
    const onEnter = () => el.setAttribute("data-lit", "true");
    const onLeave = () => el.setAttribute("data-lit", "false");

    el.addEventListener("pointermove", onMove);
    el.addEventListener("pointerenter", onEnter);
    el.addEventListener("pointerleave", onLeave);
    return () => {
      el.removeEventListener("pointermove", onMove);
      el.removeEventListener("pointerenter", onEnter);
      el.removeEventListener("pointerleave", onLeave);
    };
  }, []);

  return (
    <Tag
      ref={ref as React.Ref<never>}
      className={clsx("spotlight relative", className)}
      style={{ ["--spot-size" as string]: `${radius}px` }}
    >
      {children}
    </Tag>
  );
}
