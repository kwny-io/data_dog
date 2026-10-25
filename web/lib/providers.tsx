"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { effectiveTenant, readCookieValue, TENANT_COOKIE_NAME } from "@/lib/hints";

// ------------------------------------------------------------- query client

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        refetchOnWindowFocus: false,
        // Never retry a mutation automatically: the investigative POST is
        // expensive and must be operator-initiated.
        retry: false,
      },
      mutations: { retry: false },
    },
  });
}

let browserClient: QueryClient | undefined;

export function Providers({
  children,
  tenant,
  tenants = [],
}: {
  children: React.ReactNode;
  /** 服务端默认租户（`DD_WEB_TENANT`）。 */
  tenant: string;
  /** `DD_WEB_TENANTS` 允许清单。非空时浏览器可用 `dd_web_tenant` cookie **在清单内**选租户
   *  （由代理执行）；界面必须跟着显示真实生效的那个，否则会指向错的租户。 */
  tenants?: string[];
}) {
  const client = useMemo(() => {
    if (typeof window === "undefined") return makeQueryClient();
    browserClient ??= makeQueryClient();
    return browserClient;
  }, []);

  // SSR 阶段用服务端默认值（与服务端渲染输出一致，无 hydration 失配）；
  // 挂载后再读 cookie 算出**代理真实生效**的租户。
  const [effective, setEffective] = useState(tenant);
  useEffect(() => {
    const fromCookie = readCookieValue(document.cookie, TENANT_COOKIE_NAME);
    setEffective(effectiveTenant(tenant, tenants, fromCookie));
  }, [tenant, tenants]);

  const tenantValue = useMemo(
    () => ({
      tenant: effective,
      serverTenant: tenant,
      selectable: tenants.length > 0,
      allowed: tenants,
    }),
    [effective, tenant, tenants],
  );

  return (
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <TenantContext.Provider value={tenantValue}>{children}</TenantContext.Provider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}

// ------------------------------------------------------------------- theme

type Theme = "light" | "dark" | "system";

interface ThemeCtx {
  theme: Theme;
  resolved: "light" | "dark";
  setTheme: (t: Theme) => void;
}

const ThemeContext = createContext<ThemeCtx | null>(null);

const THEME_KEY = "dd-theme";

function readStoredTheme(): Theme {
  if (typeof window === "undefined") return "system";
  const v = window.localStorage.getItem(THEME_KEY);
  return v === "light" || v === "dark" || v === "system" ? v : "system";
}

function systemPrefersDark(): boolean {
  if (typeof window === "undefined") return false;
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(readStoredTheme);
  const [systemDark, setSystemDark] = useState<boolean>(systemPrefersDark);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (e: MediaQueryListEvent) => setSystemDark(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const resolved: "light" | "dark" = theme === "system" ? (systemDark ? "dark" : "light") : theme;

  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("dark", resolved === "dark");
  }, [resolved]);

  const setTheme = useCallback((t: Theme) => {
    setThemeState(t);
    try {
      window.localStorage.setItem(THEME_KEY, t);
    } catch {
      /* storage may be unavailable; theme still applies for the session */
    }
  }, []);

  const value = useMemo(() => ({ theme, resolved, setTheme }), [theme, resolved, setTheme]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeCtx {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used within ThemeProvider");
  return ctx;
}

/** Inline script to set the theme class before first paint (no flash). */
export const themeInitScript = `(function(){try{var t=localStorage.getItem("${THEME_KEY}")||"system";var d=t==="dark"||(t==="system"&&matchMedia("(prefers-color-scheme: dark)").matches);document.documentElement.classList.toggle("dark",d);}catch(e){}})();`;

// ------------------------------------------------------- 租户（只读展示）
//
// 这里曾有一个可写的 `TenantProvider`（localStorage 覆盖租户）+ 一个租户切换按钮。
// **已改为只读**，原因不是"没人用"，而是它现在是错的：租户由**服务端代理**
// 强制指定（`app/api/dd` 丢弃浏览器传来的 `X-Tenant-Id`，改用 `DD_WEB_TENANT`）。
// 留一个浏览器侧可改的租户开关，会向读代码的人暗示"浏览器能选租户"——
// 而在单密钥模式下，那正是租户冒充的入口，且改动其实**不会生效**（静默失效更糟）。

interface TenantCtx {
  /** **代理真实生效**的租户（cookie 在允许清单内时为它，否则为服务端默认）。展示用。 */
  tenant: string;
  /** 服务端默认租户（`DD_WEB_TENANT`）。 */
  serverTenant: string;
  /** 是否开了租户选择（`DD_WEB_TENANTS` 非空）。 */
  selectable: boolean;
  /** 允许清单本身（用于界面说清"能选哪些"）。 */
  allowed: string[];
}

const TenantContext = createContext<TenantCtx | null>(null);

export function useTenant(): TenantCtx {
  const ctx = useContext(TenantContext);
  if (!ctx) throw new Error("useTenant must be used within Providers");
  return ctx;
}
