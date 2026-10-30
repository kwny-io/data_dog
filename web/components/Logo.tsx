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
        /* `whitespace-nowrap` 是必需的，不是修饰：文本默认会在窄到一定程度时
         * 逐字断行（中文没有词边界，浏览器就一个字一行），把 56px 宽的标题列
         * 撑成 140px 高的竖条——而父级 `h-14` 是固定高度，于是溢出。
         * 窄屏直接交给外层隐藏，别让它以"先竖排再说"的方式参与争抢宽度。 */
        <span className="text-spectrum hidden whitespace-nowrap text-sm font-semibold tracking-tight sm:inline">
          遥测故障调查台
        </span>
      )}
    </span>
  );
}
