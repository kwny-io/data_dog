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

/** The chrome, in Apple's material: a translucent bar that content scrolls
 *  beneath. It carries a hairline instead of a border and no accent rule — in
 *  the system language the bar's material *is* its edge. */
function TopBar() {
  return (
    <header className="glass sticky top-0 z-30 border-b border-rule">
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

/** The sidebar is a grouped list, not a rail: items are rounded on all four
 *  corners, the active one is filled with the accent tint and its label takes
 *  the accent colour. That is the exact treatment of a selected item in System
 *  Settings, and it needs no left rule or glow to be legible. */
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
                  "block rounded-[var(--radius-token-sm)] px-3 py-2 text-sm transition-colors",
                  active
                    ? "bg-primary-soft font-medium text-primary"
                    : "text-ink-muted hover:bg-paper-sunken hover:text-ink",
                )}
              >
                {item.label}
                <span
                  className={clsx(
                    "mt-0.5 block text-[11px]",
                    active ? "text-primary/75" : "text-ink-faint",
                  )}
                >
                  {item.hint}
                </span>
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
 *  viewports it was meant to serve (see doc 38 §1.2).
 *
 *  The chips are Apple's segmented-control shape: an underlined accent bar was
 *  the old language; a filled pill is how a compact control states selection. */
function MobileNav({ pathname }: { pathname: string }) {
  return (
    <nav aria-label="主导航" className="glass border-b border-rule lg:hidden">
      <ul className="mx-auto flex w-full max-w-[1600px] gap-1 overflow-x-auto px-4 py-2">
        {NAV.map((item) => {
          const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
          return (
            <li key={item.href}>
              <Link
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={clsx(
                  "block whitespace-nowrap rounded-full px-3 py-1 text-xs transition-colors",
                  active
                    ? "bg-primary-soft font-medium text-primary"
                    : "text-ink-muted hover:bg-paper-sunken",
                )}
              >
                {item.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
