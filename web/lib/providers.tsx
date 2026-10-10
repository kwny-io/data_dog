"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

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
}: {
  children: React.ReactNode;
  /** 当前租户，由**服务端**（`resolveProxyConfig().tenant` → `DD_WEB_TENANT`）决定，
   *  只用于展示。浏览器改不了它：代理会丢弃浏览器传来的 `X-Tenant-Id`。 */
  tenant: string;
}) {
  const client = useMemo(() => {
    if (typeof window === "undefined") return makeQueryClient();
    browserClient ??= makeQueryClient();
    return browserClient;
  }, []);
  const tenantValue = useMemo(() => ({ tenant }), [tenant]);

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
  /** 服务端决定的租户（展示用；不是可切换状态）。 */
  tenant: string;
}

const TenantContext = createContext<TenantCtx | null>(null);

export function useTenant(): TenantCtx {
  const ctx = useContext(TenantContext);
  if (!ctx) throw new Error("useTenant must be used within Providers");
  return ctx;
}
