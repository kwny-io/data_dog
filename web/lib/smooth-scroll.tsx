"use client";

import { useEffect, useRef } from "react";
import { usePathname } from "next/navigation";
import Lenis from "lenis";

/**
 * Inertial ("smooth") scrolling for the console, via Lenis.
 *
 * Why Lenis and not GSAP: GSAP's value here would be orchestrating entrance
 * animation, which we already do in CSS (see .reveal / .stagger in globals.css)
 * and which the design doc explicitly warns against as a generated-page tell.
 * Lenis gives something we did NOT have — damped wheel scrolling — so it is the
 * only one of the two worth adding.
 *
 * Why this is a client island rather than a root wrapper: Lenis only needs to
 * run; it does not own the scroll container. It drives the REAL document scroll
 * (window.scrollY still moves), so sticky positioning, scrollbar, find-in-page
 * and browser scroll restoration all keep working — nothing is translate-hacked.
 * That is also why the app is safe to wrap: no layout or SSR output changes.
 *
 * Guards (the three ways this could annoy an operator, all avoided):
 *   1. prefers-reduced-motion  → never enable (damped scroll is motion).
 *   2. pointer: coarse / touch → never enable; native momentum is already the
 *      expected feel there, and Lenis-on-touch is a well-known jank source.
 *   3. route change            → resync to top, because Lenis holds its own
 *      animated target that a bare window.scrollTo(0,0) would fight.
 *
 * Nested vertical scrollers would need `data-lenis-prevent`; this app currently
 * has none (grep: no overflow-y-auto), and the only overflow-x scrollers are
 * horizontal, which Lenis's vertical orientation ignores.
 */
export function SmoothScroll() {
  const lenisRef = useRef<Lenis | null>(null);
  const pathname = usePathname();

  useEffect(() => {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const coarse = window.matchMedia("(pointer: coarse)").matches;
    if (reduce || coarse) return;

    const lenis = new Lenis({
      // A damped-but-decisive feel: operators scroll to read a precise row, so
      // this is tuned shorter than a marketing page would be. easeOutExpo
      // settles fast at the end, so a trackpad flick lands where you aimed.
      duration: 1.0,
      easing: (t: number) => Math.min(1, 1.001 - Math.pow(2, -10 * t)),
      smoothWheel: true,
    });
    lenisRef.current = lenis;

    let raf = 0;
    const loop = (time: number) => {
      lenis.raf(time);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);

    return () => {
      cancelAnimationFrame(raf);
      lenis.destroy();
      lenisRef.current = null;
    };
  }, []);

  // Land at the top on navigation. `immediate` so it is a reset, not an
  // animation from wherever the previous page was scrolled to.
  useEffect(() => {
    lenisRef.current?.scrollTo(0, { immediate: true });
  }, [pathname]);

  return null;
}
