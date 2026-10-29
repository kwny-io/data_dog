/* The substrate behind everything — ONE layer.
 *
 * It used to be four stacked strata (base wash → a blurred violet orb → grain →
 * vignette, plus a masked grid). That field was a composed scene, and a composed
 * scene behind a table of measurements competes with the measurements. The Apple
 * grouped-list answer is a single quiet surface with the depth carried by the
 * cards on top; this is that surface.
 *
 * `--bg-base` is theme-scoped in globals.css: a vertical near-white→grey wash in
 * light, flat black in dark. Server-rendered and pure CSS — no state, no effect,
 * no theme logic in JS, so there is nothing here for React to mismatch on
 * hydration.
 *
 * aria-hidden + pointer-events:none come from the .console-bg class: this layer
 * is atmosphere, never content, and must never intercept a click. */

export function ConsoleBackground() {
  return <div className="console-bg" aria-hidden />;
}
