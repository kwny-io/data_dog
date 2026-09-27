"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { DEFAULT_TENANT } from "./api/client";

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

export function Providers({ children }: { children: React.ReactNode }) {
  const client = useMemo(() => {
    if (typeof window === "undefined") return makeQueryClient();
    browserClient ??= makeQueryClient();
    return browserClient;
  }, []);

  return (
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <TenantProvider>{children}</TenantProvider>
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

// ------------------------------------------------------------------ tenant

interface TenantCtx {
  tenant: string;
  setTenant: (t: string) => void;
}

const TenantContext = createContext<TenantCtx | null>(null);

const TENANT_KEY = "dd-tenant";

/** Tenant comes from config/session, never a URL query param (spec §2).
 *  A single fixed value today; multi-tenant switching is a backend open
 *  question (spec §9.3), so this stays a local override with a warning. */
export function TenantProvider({ children }: { children: React.ReactNode }) {
  const [tenant, setTenantState] = useState<string>(() => {
    if (typeof window === "undefined") return DEFAULT_TENANT;
    return window.localStorage.getItem(TENANT_KEY) || DEFAULT_TENANT;
  });

  const setTenant = useCallback((t: string) => {
    const v = t.trim() || DEFAULT_TENANT;
    setTenantState(v);
    try {
      window.localStorage.setItem(TENANT_KEY, v);
    } catch {
      /* ignore */
    }
  }, []);

  const value = useMemo(() => ({ tenant, setTenant }), [tenant, setTenant]);
  return <TenantContext.Provider value={value}>{children}</TenantContext.Provider>;
}

export function useTenant(): TenantCtx {
  const ctx = useContext(TenantContext);
  if (!ctx) throw new Error("useTenant must be used within TenantProvider");
  return ctx;
}
