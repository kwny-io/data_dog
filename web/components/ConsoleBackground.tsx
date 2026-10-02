/* The glow field behind everything — five stacked strata, bottom to top:
 *
 *   1. .console-bg   the base wash (--bg-base: the violet-lit ramp)
 *   2. .orb ×3       soft cyan / violet / magenta pools, blurred
 *   3. .grain        a faint tiled noise that dithers the glow
 *   4. .vignette     an edge falloff, so the frame has volume
 *   5. .console-bg::after  the neon grid, radially masked toward the top-left
 *
 * Why grain and vignette are two SEPARATE elements rather than one element with
 * a pseudo: the grain is dialled down with an element `opacity`, and opacity is
 * inherited by pseudo-elements — so a vignette nested inside the grain span
 * would be crushed to the grain's strength and become invisible. Separate spans
 * each carry their own full-strength background; the softness lives in the
 * gradient's own alpha, not in the element's opacity.
 *
 * Why they sit ABOVE the orbs: the noise only does its job where the banding is
 * — on the big, low-contrast radials. Under the orbs it would miss exactly the
 * gradients it is meant to break up.
 *
 * Server-rendered and pure CSS — no state, no effect, no theme logic in JS:
 * both themes are expressed purely through the --orb-* / --bg-base / --grain-* /
 * --vignette variables in globals.css, so there is nothing here for React to
 * mismatch on hydration.
 *
 * aria-hidden + pointer-events:none come from the .console-bg class: this layer
 * is atmosphere, never content, and must never intercept a click. */

export function ConsoleBackground() {
  return (
    <div className="console-bg" aria-hidden>
      <span className="orb orb-1" />
      <span className="orb orb-2" />
      <span className="orb orb-3" />
      <span className="grain" />
      <span className="vignette" />
    </div>
  );
}
