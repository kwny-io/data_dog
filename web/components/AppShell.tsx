"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { ThemeToggle } from "@/components/ThemeToggle";
import { TechToggle } from "@/components/TechToggle";
import { TenantBadge } from "@/components/TenantBadge";
import { HealthBadge } from "@/components/HealthBadge";
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
      <div className="mx-auto flex w-full max-w-[1600px] gap-0 px-4 lg:px-6">
        <SideNav pathname={pathname} />
        <main className="min-w-0 flex-1 py-5 lg:pl-6">{children}</main>
      </div>
    </div>
  );
}

function TopBar() {
  return (
    <header className="sticky top-0 z-30 border-b border-rule-strong bg-panel/95 backdrop-blur supports-[backdrop-filter]:bg-panel/85">
      <div className="mx-auto flex h-14 w-full max-w-[1600px] items-center gap-4 px-4 lg:px-6">
        <Link href="/" className="flex items-baseline gap-2">
          <span className="reading text-sm font-semibold tracking-tight text-ink">data_dog</span>
          <span className="hidden text-xs text-ink-faint sm:inline">遥测故障调查台</span>
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
      <ul className="space-y-0.5">
        {NAV.map((item) => {
          const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
          return (
            <li key={item.href}>
              <Link
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={clsx(
                  // Nav is a distinct interaction plane: a persistent left rule
                  // marks it as a link even at rest, so it never reads as plain
                  // text. Colour is the active/hover enhancement, not the cue.
                  "block border-l-2 px-3 py-2 text-sm transition-colors",
                  active
                    ? "border-primary bg-primary-soft font-medium text-primary"
                    : "border-rule-strong text-ink-muted hover:border-primary hover:bg-paper-sunken hover:text-ink",
                )}
              >
                {item.label}
                <span className="mt-0.5 block text-[11px] text-ink-faint">{item.hint}</span>
              </Link>
            </li>
          );
        })}
      </ul>
      <MobileNav pathname={pathname} />
    </nav>
  );
}

/** Below lg the sidebar is hidden; a horizontal strip keeps navigation usable. */
function MobileNav({ pathname }: { pathname: string }) {
  return (
    <ul className="mt-3 flex gap-1 overflow-x-auto lg:hidden">
      {NAV.map((item) => {
        const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
        return (
          <li key={item.href}>
            <Link
              href={item.href}
              className={clsx(
                "whitespace-nowrap rounded-[var(--radius-token)] px-2.5 py-1 text-xs",
                active ? "bg-primary-soft font-medium text-primary" : "text-ink-muted",
              )}
            >
              {item.label}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
