/* The mark: a hex frame carrying a rising telemetry pulse — the product's
 * whole story in one glyph (a device signal, read).
 *
 * The glyph is drawn in one flat accent colour. It used to be a three-stop
 * cyan→violet→magenta gradient with the wordmark set in the same spectrum; both
 * are gone. A gradient logo is an identity that competes with every other colour
 * on the page, and this console's colours are data. Apple's own marks are
 * monochrome for the same reason — the accent is spent on the one thing the
 * user can act on, not on the brand.
 *
 * Pure SVG + CSS, no state — safe as a server component on every route. */

export function Logo({ compact = false }: { compact?: boolean }) {
  return (
    <span className="flex items-center gap-2.5">
      <span className="grid h-8 w-8 shrink-0 place-items-center rounded-[8px] border border-rule bg-panel">
        <svg
          viewBox="0 0 24 24"
          className="h-4.5 w-4.5 text-primary"
          fill="none"
          aria-hidden
          width="18"
          height="18"
          stroke="currentColor"
        >
          {/* hex frame */}
          <path d="M12 2.2 20.5 7v10L12 21.8 3.5 17V7z" strokeWidth="1.6" strokeLinejoin="round" />
          {/* signal pulse */}
          <path
            d="M7 13.4h2.3l1.5-3.4 2 6 1.6-4.2 1.1 1.6H17"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </span>
      {!compact && (
        <span className="text-sm font-semibold tracking-tight text-ink">遥测故障调查台</span>
      )}
    </span>
  );
}
