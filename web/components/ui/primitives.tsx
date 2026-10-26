import clsx from "clsx";

import { TONE_CLASS, type Tone } from "@/lib/domain";
import { HScroll } from "@/components/ui/hscroll";

/* ------------------------------------------------------------------ Panel *
 * A holographic plate: frosted glass over the orb field, soft rounded corners,
 * a spectrum shimmer along the top edge, and a cool halo. Hierarchy comes from
 * glow weight and the top rule, not from stacked drop shadows.
 *
 * The header is treated as its own stratum, not a title bar:
 *   - a short spectrum *rake* at the left edge marks the section the way a nav
 *     rail and the logo frame do — one ornament language across the console;
 *   - the header separates from the body with a hairline that fades out to the
 *     right, so it never competes with the top-edge shimmer for attention.
 *
 * No pointer-tracked spotlight: a highlight following the cursor across every
 * panel made the console read as a demo rather than an instrument. The border,
 * the top rule and the hover lift carry the affordance; nothing needs to follow
 * the mouse. */

export function Panel({
  children,
  className,
  title,
  subtitle,
  actions,
  accent = true,
  lift = true,
  variant = "plate",
  aside,
  as: Tag = "section",
}: {
  children: React.ReactNode;
  className?: string;
  title?: React.ReactNode;
  subtitle?: React.ReactNode;
  actions?: React.ReactNode;
  /** A fixed aside, pinned to the right of the header (e.g. a status chip).
   *  Distinct from `actions`, which holds controls. */
  aside?: React.ReactNode;
  /** Draw the spectrum shimmer along the top edge. On by default. */
  accent?: boolean;
  /** Warm the border + deepen the halo on hover. Off for nested/static plates. */
  lift?: boolean;
  /** `plate` is the full holographic surface (glass + halo + border).
   *  `quiet` drops all three for a thin, borderless field: a low-alpha tint and
   *  a single hairline above the header, nothing else. Use it for panels that
   *  are supporting detail rather than the view's focal point — ten equally-lit
   *  plates on one screen have no hierarchy, which reads as weight, not depth. */
  variant?: "plate" | "quiet";
  as?: "section" | "div" | "article";
}) {
  const quiet = variant === "quiet";
  return (
    <Tag
      className={clsx(
        // `relative` is explicit rather than incidental: the accent span is
        // absolutely positioned. `.glass` happens to establish a containing
        // block via backdrop-filter, but relying on that would tie the accent's
        // geometry to a filter detail.
        "relative overflow-hidden rounded-[var(--radius-token)]",
        quiet
          ? "bg-panel/40 ring-1 ring-rule/40"
          : clsx("glass glow border border-rule", lift && "lift"),
        className,
      )}
    >
      {accent && !quiet && (
        <span
          aria-hidden
          className="pointer-events-none absolute inset-x-0 top-0 z-10 h-px bg-gradient-to-r from-transparent via-spec-violet/60 to-transparent"
        />
      )}
      {(title || actions || aside) && (
        <header className="relative z-[1] flex items-baseline justify-between gap-4 border-b border-rule px-4 py-2.5">
          <div className="panel-rake min-w-0 pl-2.5">
            {title && (
              <h2 className="text-[13px] font-semibold tracking-wide text-ink">{title}</h2>
            )}
            {subtitle && <p className="mt-0.5 text-xs text-ink-muted">{subtitle}</p>}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {aside}
            {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
          </div>
        </header>
      )}
      {children}
    </Tag>
  );
}

/* ------------------------------------------------------------- MetricTile *
 * An instrument read-out: label small above, value large in mono below,
 * unit/context as a suffix. The mono face is what signals "this is a
 * measurement"; the value carries the tone colour and a soft glow. */

export function MetricTile({
  label,
  value,
  unit,
  hint,
  tone = "primary",
  pending,
  hero = false,
  className,
}: {
  label: string;
  /** Required unless `pending` is set. */
  value?: React.ReactNode;
  unit?: string;
  hint?: React.ReactNode;
  tone?: Tone;
  /** When true, the tile renders the pending shape instead of a value. */
  pending?: boolean;
  /** Render as a bare read-out on the page substrate: no plate, no border, no
   *  halo, and the value at display size. Exactly one of these per view is the
   *  point — it is what the eye should land on first. Everything else stays a
   *  plate, so the hierarchy is stated by contrast rather than by size alone. */
  hero?: boolean;
  className?: string;
}) {
  const c = TONE_CLASS[tone];

  if (hero) {
    return (
      <div className={clsx("px-1 py-1", className)}>
        <div className="eyebrow">{label}</div>
        {pending ? (
          <span className="reading mt-2 block text-[64px] font-semibold leading-none text-ink-faint">
            —
          </span>
        ) : (
          <div className="mt-2 flex items-baseline gap-2">
            <span
              className={clsx(
                "reading text-[64px] font-semibold leading-[0.9] tracking-tight",
                c.text,
              )}
            >
              {value}
            </span>
            {unit && <span className="text-sm text-ink-muted">{unit}</span>}
          </div>
        )}
        <span aria-hidden className={clsx("mt-3 block h-0.5 w-12 rounded-full bg-current", c.text)} />
        {hint && <div className="mt-2 text-xs text-ink-faint">{hint}</div>}
      </div>
    );
  }

  return (
    <Panel accent={false} lift className={clsx("px-4 py-3", className)}>
      {/* A reading instrument: a tone-coloured datum bar sits under the value,
          so the tile's state is legible from the bar's colour alone even before
          the number is read — the same "signal, not decoration" rule. */}
      <div className="eyebrow">{label}</div>
      {pending ? (
        <div className="mt-1.5">
          <span className="reading text-[26px] font-semibold leading-none text-ink-faint">—</span>
          <span className="reading ml-2 align-middle text-[10px] text-ink-faint">待接入</span>
        </div>
      ) : (
        <div className="mt-1.5 flex items-baseline gap-1.5">
          <span className={clsx("reading text-[26px] font-semibold leading-none", c.text)}>
            {value}
          </span>
          {unit && <span className="text-xs text-ink-muted">{unit}</span>}
        </div>
      )}
      <span aria-hidden className={clsx("mt-2 block h-0.5 w-8 rounded-full bg-current", c.text)} />
      {hint && <div className="mt-1.5 text-xs text-ink-faint">{hint}</div>}
    </Panel>
  );
}

/* ------------------------------------------------------------------ Badge */

export function Badge({
  tone = "suppressed",
  children,
  dot,
  filled = false,
  className,
}: {
  tone?: Tone;
  children: React.ReactNode;
  dot?: boolean;
  filled?: boolean;
  className?: string;
}) {
  const c = TONE_CLASS[tone];
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium",
        c.text,
        c.border,
        filled ? c.bg : "bg-panel/40",
        filled && "shadow-[0_0_12px_-2px_currentColor]",
        className,
      )}
    >
      {dot && (
        <span
          aria-hidden
          className={clsx("h-1.5 w-1.5 rounded-full", filled ? "bg-current" : "border border-current")}
        />
      )}
      {children}
    </span>
  );
}

/* ------------------------------------------------------------ PendingSlot *
 * A *designed* not-yet-wired state. When a panel's data source has no endpoint
 * yet, the enterprise-grade answer is not an apology in the header ("尚未实现,
 * 本页不编造数据") — it is to build the real information architecture now and
 * mark the slot with the shape the data will take, the exact source, and how to
 * reproduce it. The frame is drawn as a dashed hairline so a gap reads as
 * "a measured field awaiting its value", not as an error or an oversight.
 *
 * Deliberately holds NO numbers: representing pending data as a zero, a dash in
 * a data cell, or a chart with an invented series are all lies of different
 * sizes. This primitive makes the absence legible instead. */

export function PendingSlot({
  title,
  will,
  source,
  command,
  className,
}: {
  /** What this slot is (e.g. "通过率趋势"). */
  title: string;
  /** What will appear here once wired. */
  will: React.ReactNode;
  /** Where the data comes from (module path / endpoint). */
  source?: string;
  /** Reproduce command, shown in mono. */
  command?: string;
  className?: string;
}) {
  return (
    <div
      className={clsx(
        "flex flex-col gap-1.5 rounded-[var(--radius-token)] border border-dashed border-rule-strong bg-panel/30 px-3 py-2.5",
        className,
      )}
      role="group"
      aria-label={`${title}：数据源未接入`}
    >
      <div className="flex items-center gap-2">
        <span
          aria-hidden
          className="inline-block h-1.5 w-1.5 shrink-0 rotate-45 border border-ink-faint"
        />
        <span className="text-xs font-medium text-ink-muted">{title}</span>
        <span className="reading ml-auto text-[10px] uppercase tracking-wide text-ink-faint">
          数据源未接入
        </span>
      </div>
      <p className="text-xs leading-relaxed text-ink-muted">{will}</p>
      {source && (
        <p className="reading break-all text-[11px] text-ink-faint">
          {source}
        </p>
      )}
      {command && (
        <code className="reading mt-0.5 block break-all rounded-r-[var(--radius-token)] border-l-2 border-spec-violet/50 pl-2.5 text-[11px] text-ink">
          {command}
        </code>
      )}
    </div>
  );
}

/* -------------------------------------------------------------- Skeletons */

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={clsx("skeleton-shimmer rounded-[var(--radius-token)]", className)}
    />
  );
}

export function SkeletonRows({ rows = 5, className }: { rows?: number; className?: string }) {
  return (
    <div className={clsx("space-y-2 p-4", className)} role="status" aria-label="加载中">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-6 w-full" />
      ))}
    </div>
  );
}

/* ------------------------------------------------------------ Empty/Error *
 * Emptiness is an invitation to act, not a blank. Every empty state names
 * the next step. */

export function EmptyState({
  title,
  hint,
  action,
  className,
  icon,
}: {
  title: string;
  hint?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
  icon?: React.ReactNode;
}) {
  return (
    <div className={clsx("flex flex-col items-center justify-center px-6 py-14 text-center", className)}>
      {icon && <div className="mb-3 text-ink-faint">{icon}</div>}
      <p className="text-sm font-medium text-ink">{title}</p>
      {hint && <p className="mt-1.5 max-w-md text-xs leading-relaxed text-ink-muted">{hint}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorState({
  title,
  hint,
  technical,
  onRetry,
  className,
}: {
  title: string;
  hint?: string;
  technical?: string | null;
  onRetry?: () => void;
  className?: string;
}) {
  return (
    <div className={clsx("rounded-[var(--radius-token)] border-l-2 border-critical bg-critical-soft/50 px-5 py-4", className)} role="alert">
      <p className="text-sm font-semibold text-ink">{title}</p>
      {hint && <p className="mt-1 text-xs leading-relaxed text-ink-muted">{hint}</p>}
      {technical && (
        <p className="reading mt-2 break-all text-[11px] text-ink-faint">{technical}</p>
      )}
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 inline-flex items-center rounded-[var(--radius-token)] border border-rule-strong bg-panel/60 px-3 py-1 text-xs font-medium text-ink hover:bg-paper-sunken"
        >
          重试
        </button>
      )}
    </div>
  );
}

/* ----------------------------------------------------------------- Button */

export function Button({
  variant = "secondary",
  size = "md",
  className,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
}) {
  const variants = {
    primary:
      "bg-spectrum text-on-primary border-transparent shadow-[0_0_22px_-6px_var(--glow-ring)] hover:brightness-110",
    secondary: "bg-panel/50 text-ink border-rule-strong hover:bg-paper-sunken",
    ghost: "bg-transparent text-ink-muted border-transparent hover:bg-paper-sunken hover:text-ink",
    danger: "bg-panel/50 text-critical border-critical hover:bg-critical-soft",
  } as const;
  const sizes = { sm: "px-2.5 py-1 text-xs", md: "px-3.5 py-1.5 text-sm" } as const;
  return (
    <button
      {...props}
      className={clsx(
        "relative inline-flex items-center justify-center gap-1.5 rounded-[var(--radius-token)] border font-medium transition-all",
        "disabled:cursor-not-allowed disabled:opacity-50",
        variants[variant],
        sizes[size],
        className,
      )}
    />
  );
}

/* --------------------------------------------------------------- Chevron *
 * A persistent disclosure marker for a clickable row. Always drawn, so the
 * affordance is visible without hover (keyboard/touch users never hover, and
 * hover-only cues make everyone guess wrong once to learn the row is live). */

export function RowChevron({ className }: { className?: string }) {
  return (
    <svg
      aria-hidden
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      className={clsx("shrink-0", className)}
    >
      <path d="m9 6 6 6-6 6" />
    </svg>
  );
}

/* -------------------------------------------------------------- Pagination */

export function Pagination({
  count,
  total,
  limit,
  offset,
  hasMore,
  onChange,
  className,
}: {
  count: number;
  total: number;
  limit: number;
  offset: number;
  hasMore: boolean;
  onChange: (offset: number) => void;
  className?: string;
}) {
  const from = total === 0 ? 0 : offset + 1;
  const to = offset + count;
  return (
    <nav
      className={clsx(
        "flex items-center justify-between gap-3 border-t border-rule px-4 py-2.5 text-xs",
        className,
      )}
      aria-label="分页"
    >
      <span className="reading text-ink-muted">
        {from}–{to} / 共 {total}
      </span>
      <div className="flex items-center gap-1.5">
        <Button
          size="sm"
          onClick={() => onChange(Math.max(0, offset - limit))}
          disabled={offset <= 0}
        >
          上一页
        </Button>
        <Button
          size="sm"
          onClick={() => onChange(offset + limit)}
          disabled={!hasMore}
        >
          下一页
        </Button>
      </div>
    </nav>
  );
}

/* ---------------------------------------------------------------- DataTable *
 * Semantic table: real <table>, headers with scope, so screen readers and
 * keyboard users get the structure. Numeric cells use the mono reading face. */

export function DataTable({
  head,
  children,
  className,
}: {
  head: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <HScroll className={className}>
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-rule-strong text-left text-xs text-ink-muted">{head}</tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </HScroll>
  );
}

export function Th({
  children,
  numeric,
  sticky,
  className,
  scope = "col",
}: {
  children?: React.ReactNode;
  numeric?: boolean;
  /** Pin this header cell while the table scrolls horizontally. */
  sticky?: boolean;
  className?: string;
  scope?: "col" | "row";
}) {
  return (
    <th
      scope={scope}
      className={clsx(
        "whitespace-nowrap px-3 py-2 font-medium",
        numeric && "text-right",
        sticky && "sticky left-0 z-20 border-r border-rule bg-panel/90 backdrop-blur",
        className,
      )}
    >
      {children}
    </th>
  );
}

export function Td({
  children,
  numeric,
  sticky,
  className,
}: {
  children?: React.ReactNode;
  numeric?: boolean;
  /** Pin this cell while the table scrolls horizontally. Needs an opaque
   *  background or rows scroll *under* it; `group-hover` keeps the row-hover
   *  cue alive for the pinned column too. */
  sticky?: boolean;
  className?: string;
}) {
  return (
    <td
      className={clsx(
        "px-3 py-2 align-middle text-ink",
        numeric && "reading text-right",
        sticky && "sticky left-0 z-10 border-r border-rule bg-panel/90 backdrop-blur group-hover:bg-paper-sunken",
        className,
      )}
    >
      {children}
    </td>
  );
}

/* ---------------------------------------------------------------- Toolbar *
 * A quiet strip above a panel body: filter chips / segmented controls. */

export function Toolbar({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={clsx("flex flex-wrap items-center gap-2 border-b border-rule px-4 py-2.5", className)}>
      {children}
    </div>
  );
}
