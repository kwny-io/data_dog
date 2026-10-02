"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { ThemeToggle } from "@/components/ThemeToggle";
import { TechToggle } from "@/components/TechToggle";
import { TenantBadge } from "@/components/TenantBadge";
import { HealthBadge } from "@/components/HealthBadge";
import { Logo } from "@/components/Logo";
import clsx from "clsx";

const NAV = [
  { href: "/", label: "仪表盘", hint: "服务健康与业务量" },
  { href: "/investigate", label: "调查台", hint: "候选 → 调查 → 报告" },
  { href: "/alerts", label: "告警", hint: "状态机与投递" },
  { href: "/operators", label: "算子", hint: "检测算子清单" },
  { href: "/evals", label: "评估", hint: "离线评估结果" },
] as const;

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="min-h-dvh">
      <TopBar />
      <MobileNav pathname={pathname} />
      <div className="mx-auto flex w-full max-w-[1600px] gap-0 px-4 lg:px-6">
        <SideNav pathname={pathname} />
        <main className="min-w-0 flex-1 py-5 lg:pl-6">{children}</main>
      </div>
    </div>
  );
}

function TopBar() {
  return (
    <header className="glass sticky top-0 z-30 border-b border-rule">
      <span
        aria-hidden
        className="flow-line pointer-events-none absolute inset-x-0 bottom-[-1px] h-px opacity-70"
      />
      <div className="mx-auto flex h-14 w-full max-w-[1600px] items-center gap-4 px-4 lg:px-6">
        <Link href="/" aria-label="遥测故障调查台">
          <Logo />
        </Link>
        <div className="ml-auto flex items-center gap-2">
          <TenantBadge />
          <TechToggle />
          <HealthBadge />
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}

function SideNav({ pathname }: { pathname: string }) {
  return (
    <nav aria-label="主导航" className="hidden w-52 shrink-0 py-5 lg:block">
      <ul className="space-y-1">
        {NAV.map((item) => {
          const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
          return (
            <li key={item.href}>
              <Link
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={clsx(
                  // Nav is a distinct interaction plane: a persistent left rail
                  // marks it as a link even at rest, so it never reads as plain
                  // text. The active state lights the rail with a travelling
                  // shimmer (flow-line) — the same "energised edge" as the top
                  // bar and the primary CTA, so live things share one signature.
                  "relative block rounded-r-[var(--radius-token)] border-l-2 px-3 py-2 text-sm transition-all",
                  active
                    ? "border-spec-violet bg-primary-soft font-medium text-primary shadow-[inset_0_0_20px_-8px_var(--glow-ring)]"
                    : "border-rule-strong text-ink-muted hover:border-spec-cyan hover:bg-paper-sunken hover:text-ink",
                )}
              >
                {active && (
                  <span
                    aria-hidden
                    className="flow-line pointer-events-none absolute inset-y-1.5 left-[-2px] w-0.5 rounded-full"
                  />
                )}
                {item.label}
                <span className="mt-0.5 block text-[11px] text-ink-faint">{item.hint}</span>
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

/** Below lg the sidebar is hidden, so navigation moves to a horizontal strip
 *  directly under the top bar. It must live OUTSIDE SideNav — nesting it there
 *  put it inside a `hidden … lg:block` parent, which hid it on exactly the
 *  viewports it was meant to serve (see doc 38 §1.2). */
function MobileNav({ pathname }: { pathname: string }) {
  return (
    <nav
      aria-label="主导航"
      className="glass border-b border-rule lg:hidden"
    >
      <ul className="mx-auto flex w-full max-w-[1600px] gap-1 overflow-x-auto px-4 py-2">
        {NAV.map((item) => {
          const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
          return (
            <li key={item.href}>
              <Link
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={clsx(
                  "relative block whitespace-nowrap rounded-full px-2.5 py-1 text-xs transition-colors",
                  active
                    ? "bg-gradient-to-r from-spec-violet/25 to-spec-magenta/20 font-medium text-primary"
                    : "text-ink-muted",
                )}
              >
                {item.label}
                {active && (
                  <span
                    aria-hidden
                    className="pointer-events-none absolute inset-x-2 -bottom-px h-px rounded-full bg-gradient-to-r from-transparent via-spec-violet to-transparent"
                  />
                )}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
