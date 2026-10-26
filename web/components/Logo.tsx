/* The mark: a hex frame carrying a rising telemetry pulse — the product's
 * whole story in one glyph (a device signal, read). The plate around it is a
 * static hairline; the gradient lives in the glyph itself, so the identity is
 * carried by the drawing rather than by an animation running around it.
 *
 * Pure SVG + CSS, no state — safe as a server component on every route. */

export function Logo({ compact = false }: { compact?: boolean }) {
  return (
    <span className="flex items-center gap-2.5">
      <span className="grid h-8 w-8 shrink-0 place-items-center rounded-[8px] border border-rule-strong bg-panel/60">
        <svg
          viewBox="0 0 24 24"
          className="h-4.5 w-4.5"
          fill="none"
          aria-hidden
          width="18"
          height="18"
        >
          <defs>
            <linearGradient id="dd-mark" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor="var(--spec-cyan)" />
              <stop offset="55%" stopColor="var(--spec-violet)" />
              <stop offset="100%" stopColor="var(--spec-magenta)" />
            </linearGradient>
          </defs>
          {/* hex frame */}
          <path
            d="M12 2.2 20.5 7v10L12 21.8 3.5 17V7z"
            stroke="url(#dd-mark)"
            strokeWidth="1.6"
            strokeLinejoin="round"
          />
          {/* signal pulse */}
          <path
            d="M7 13.4h2.3l1.5-3.4 2 6 1.6-4.2 1.1 1.6H17"
            stroke="url(#dd-mark)"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </span>
      {!compact && (
        <span className="text-spectrum text-sm font-semibold tracking-tight">遥测故障调查台</span>
      )}
    </span>
  );
}
