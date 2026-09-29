import clsx from "clsx";

import { TONE_CLASS, type Tone } from "@/lib/domain";
import { HScroll } from "@/components/ui/hscroll";

/* ------------------------------------------------------------------ Panel *
 * A drawing sheet: square corners, a hairline rule, a stronger frame, and
 * four corner ticks. Hierarchy comes from line weight, not from shadows —
 * the SaaS rounded-card-with-soft-shadow kit is deliberately avoided. */

export function Panel({
  children,
  className,
  title,
  subtitle,
  actions,
  ticks = true,
  as: Tag = "section",
}: {
  children: React.ReactNode;
  className?: string;
  title?: React.ReactNode;
  subtitle?: React.ReactNode;
  actions?: React.ReactNode;
  ticks?: boolean;
  as?: "section" | "div" | "article";
}) {
  return (
    <Tag
      className={clsx(
        "relative border border-rule-strong bg-panel",
        "rounded-[var(--radius-token)]",
        className,
      )}
    >
      {ticks && (
        <>
          <Tick className="left-[-1px] top-[-1px] border-l-2 border-t-2" />
          <Tick className="right-[-1px] top-[-1px] border-r-2 border-t-2" />
          <Tick className="bottom-[-1px] left-[-1px] border-b-2 border-l-2" />
          <Tick className="bottom-[-1px] right-[-1px] border-b-2 border-r-2" />
        </>
      )}
      {(title || actions) && (
        <header className="flex items-baseline justify-between gap-4 border-b border-rule px-4 py-2.5">
          <div className="min-w-0">
            {title && (
              <h2 className="text-[13px] font-semibold tracking-wide text-ink">
                {title}
              </h2>
            )}
            {subtitle && <p className="mt-0.5 text-xs text-ink-muted">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      {children}
    </Tag>
  );
}

function Tick({ className }: { className: string }) {
  return (
    <span
      aria-hidden
      className={clsx("pointer-events-none absolute h-2 w-2 border-rule-strong", className)}
    />
  );
}

/* ------------------------------------------------------------- MetricTile *
 * An instrument read-out: label small above, value large in mono below,
 * unit/context as a suffix. Right-aligned numbers would fight the label, so
 * value leads left; the mono face is what signals "this is a measurement". */

export function MetricTile({
  label,
  value,
  unit,
  hint,
  tone = "primary",
  pending,
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
  className?: string;
}) {
  return (
    <Panel className={clsx("px-4 py-3", className)}>
      <div className="text-xs font-medium text-ink-muted">{label}</div>
      {pending ? (
        <div className="mt-1.5">
          <span className="reading text-[26px] font-semibold leading-none text-ink-faint">—</span>
          <span className="ml-2 align-middle text-[10px] uppercase tracking-wide text-ink-faint">
            待接入
          </span>
        </div>
      ) : (
        <div className="mt-1.5 flex items-baseline gap-1.5">
          <span className={clsx("reading text-[26px] font-semibold leading-none", TONE_CLASS[tone].text)}>
            {value}
          </span>
          {unit && <span className="text-xs text-ink-muted">{unit}</span>}
        </div>
      )}
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
        "inline-flex items-center gap-1.5 rounded-[var(--radius-token)] border px-2 py-0.5 text-xs font-medium",
        c.text,
        c.border,
        filled ? c.bg : "bg-transparent",
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
        "flex flex-col gap-1.5 border border-dashed border-rule-strong px-3 py-2.5",
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
        <code className="reading mt-0.5 block break-all border-l-2 border-rule pl-2.5 text-[11px] text-ink">
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
      className={clsx("animate-pulse rounded-[var(--radius-token)] bg-paper-sunken", className)}
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
    <div className={clsx("border-l-2 border-critical bg-critical-soft/40 px-5 py-4", className)} role="alert">
      <p className="text-sm font-semibold text-ink">{title}</p>
      {hint && <p className="mt-1 text-xs leading-relaxed text-ink-muted">{hint}</p>}
      {technical && (
        <p className="reading mt-2 break-all text-[11px] text-ink-faint">{technical}</p>
      )}
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 inline-flex items-center border border-rule-strong bg-panel px-3 py-1 text-xs font-medium text-ink hover:bg-paper-sunken"
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
    primary: "bg-primary text-on-primary border-primary hover:bg-primary-hover",
    secondary: "bg-panel text-ink border-rule-strong hover:bg-paper-sunken",
    ghost: "bg-transparent text-ink-muted border-transparent hover:bg-paper-sunken hover:text-ink",
    danger: "bg-panel text-critical border-critical hover:bg-critical-soft",
  } as const;
  const sizes = { sm: "px-2.5 py-1 text-xs", md: "px-3.5 py-1.5 text-sm" } as const;
  return (
    <button
      {...props}
      className={clsx(
        "inline-flex items-center justify-center gap-1.5 rounded-[var(--radius-token)] border font-medium transition-colors",
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
        sticky && "sticky left-0 z-20 border-r border-rule bg-panel",
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
        sticky && "sticky left-0 z-10 border-r border-rule bg-panel group-hover:bg-paper-sunken",
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
