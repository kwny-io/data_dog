/* The substrate behind everything — four stacked strata, bottom to top:
 *
 *   1. .console-bg   the base wash (--bg-base)
 *   2. .orb          ONE soft pool of light, top-left, static
 *   3. .grain        a faint tiled noise that dithers the gradient
 *   4. .vignette     an edge falloff, so the frame has volume
 *   5. .console-bg::after  the faint grid, radially masked toward the top-left
 *
 * Why one orb instead of three: three overlapping pools at 0.4–0.6 alpha made
 * the substrate read as fog, and text sat on a moving wash. Depth comes from the
 * gradients themselves, not from a moving light show behind the data.
 *
 * Why grain and vignette are two SEPARATE elements rather than one element with
 * a pseudo: the grain is dialled down with an element `opacity`, and opacity is
 * inherited by pseudo-elements — so a vignette nested inside the grain span
 * would be crushed to the grain's strength and become invisible. Separate spans
 * each carry their own full-strength background; the softness lives in the
 * gradient's own alpha, not in the element's opacity.
 *
 * Server-rendered and pure CSS — no state, no effect, no theme logic in JS:
 * both themes are expressed purely through the --orb-1 / --bg-base / --grain-* /
 * --vignette variables in globals.css, so there is nothing here for React to
 * mismatch on hydration.
 *
 * aria-hidden + pointer-events:none come from the .console-bg class: this layer
 * is atmosphere, never content, and must never intercept a click. */

export function ConsoleBackground() {
  return (
    <div className="console-bg" aria-hidden>
      <span className="orb orb-1" />
      <span className="grain" />
      <span className="vignette" />
    </div>
  );
}
