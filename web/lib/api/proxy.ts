/**
 * Server-side proxy for the data_dog API — the console's only credentialed path.
 *
 * Why this module exists (security)
 * ---------------------------------
 * Next.js **inlines every `NEXT_PUBLIC_*` value into the browser bundle**. The
 * console used to read `NEXT_PUBLIC_API_KEY` and set `X-API-Key` from the
 * browser, so anyone who opened devtools could read a working API key. Combined
 * with the backend's single global key (which spans **all** tenants until
 * `DD_TENANT_KEYS` is configured — see README 诚实边界), a leaked console key is
 * tenant impersonation, not merely a leaked read token.
 *
 * The fix is structural: the browser talks to `/api/dd/*` on its **own origin**
 * and never carries credentials. This module (server-only) attaches
 * `X-API-Key` / `X-Tenant-Id` from **server** environment variables and forwards
 * the request. Two consequences worth stating explicitly:
 *
 *  - `X-Tenant-Id` is **forced from server config** and any client-supplied
 *    value is dropped. Otherwise the browser could still pick a tenant (harmless
 *    with tenant-bound keys, but impersonation with a global key).
 *  - The console itself becomes a privileged surface, so it is gated: set
 *    `DD_WEB_BASIC_AUTH=user:pass`, or explicitly opt out for local use with
 *    `DD_WEB_ALLOW_ANONYMOUS=1`. If a key is configured and neither is set, the
 *    proxy **fails closed** rather than exposing an unauthenticated console.
 */

export interface ProxyConfig {
  /** Upstream data_dog service (server-side address; never sent to the browser). */
  baseUrl: string;
  /** Server-only API key forwarded as `X-API-Key`. Empty = backend has no auth. */
  apiKey: string;
  /** Tenant forced onto every request. Client input is ignored. */
  tenant: string;
  /** `user:pass` gate for the console itself. Empty = no gate configured. */
  basicAuth: string;
  /** Explicit, auditable opt-out used when running locally without a gate. */
  allowAnonymous: boolean;
}

export interface AuthOk {
  ok: true;
}
export interface AuthDenied {
  ok: false;
  status: number;
  message: string;
  headers?: Record<string, string>;
}
export type AuthResult = AuthOk | AuthDenied;

const DEFAULT_BASE_URL = "http://127.0.0.1:8000";
const DEFAULT_TENANT = "tenant-demo";

/** Paths the console is allowed to reach through the proxy. */
const ALLOWED_EXACT = new Set(["healthz", "readyz", "metrics"]);

export function resolveProxyConfig(
  env: Record<string, string | undefined> = process.env,
): ProxyConfig {
  return {
    baseUrl: (env.DD_WEB_API_BASE_URL ?? DEFAULT_BASE_URL).replace(/\/+$/, ""),
    apiKey: (env.DD_WEB_API_KEY ?? "").trim(),
    tenant: (env.DD_WEB_TENANT ?? DEFAULT_TENANT).trim() || DEFAULT_TENANT,
    basicAuth: (env.DD_WEB_BASIC_AUTH ?? "").trim(),
    allowAnonymous: (env.DD_WEB_ALLOW_ANONYMOUS ?? "").trim() === "1",
  };
}

/**
 * Validate and join the catch-all path segments.
 *
 * Returns the upstream path (`v1/...`, `metrics`, `healthz`, `readyz`) or `null`
 * when the request must be refused. This is what stops the proxy from becoming
 * an open relay: only the service's own API surface is reachable, and traversal
 * (`..`, backslashes, encoded dots) never survives to the upstream URL.
 */
export function sanitizeApiPath(segments: string[] | undefined): string | null {
  if (!segments || segments.length === 0) return null;
  const parts: string[] = [];
  for (const raw of segments) {
    if (typeof raw !== "string" || raw.length === 0) return null;
    const seg = raw.toLowerCase();
    if (seg === "." || seg === "..") return null;
    if (seg.includes("..") || raw.includes("\\") || raw.includes("\0")) return null;
    if (/[?#]/.test(raw)) return null;
    if (!/^[A-Za-z0-9._~-]+$/.test(raw)) return null;
    parts.push(raw);
  }
  const path = parts.join("/");
  if (ALLOWED_EXACT.has(path)) return path;
  if (path.startsWith("v1/") && parts.length >= 2) return path;
  return null;
}

/** Constant-time-ish comparison (avoids the cheapest timing oracles). */
export function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/**
 * Gate the console itself.
 *
 * Precedence: explicit Basic gate → fail-closed when a backend key is present
 * without a gate → open (no key configured: local development).
 */
export function checkConsoleAuth(
  authorization: string | null,
  cfg: ProxyConfig,
): AuthResult {
  if (cfg.basicAuth) {
    const expected = `Basic ${Buffer.from(cfg.basicAuth, "utf8").toString("base64")}`;
    if (!authorization || !safeEqual(authorization.trim(), expected)) {
      return {
        ok: false,
        status: 401,
        message: "控制台需要 Basic 认证（DD_WEB_BASIC_AUTH）",
        headers: { "WWW-Authenticate": 'Basic realm="data_dog console", charset="UTF-8"' },
      };
    }
    return { ok: true };
  }
  if (cfg.apiKey && !cfg.allowAnonymous) {
    return {
      ok: false,
      status: 503,
      message:
        "控制台已配置后端密钥（DD_WEB_API_KEY）但没有用户认证：拒绝提供服务以免把特权控制台" +
        "暴露给任何人。请设置 DD_WEB_BASIC_AUTH=user:pass（推荐，或置于带认证的反代之后），" +
        "本机开发可显式设 DD_WEB_ALLOW_ANONYMOUS=1。",
    };
  }
  return { ok: true };
}

export function buildUpstreamUrl(cfg: ProxyConfig, apiPath: string, search = ""): string {
  const qs = search && search.startsWith("?") ? search : search ? `?${search}` : "";
  return `${cfg.baseUrl}/${apiPath}${qs}`;
}

/** Headers that may be forwarded from the browser (allow-list, not deny-list). */
const FORWARD_REQUEST_HEADERS = [
  "accept",
  "content-type",
  "x-request-id",
  "x-device-sn",
  "traceparent",
];

export function buildUpstreamHeaders(incoming: Headers, cfg: ProxyConfig): Headers {
  const out = new Headers();
  for (const name of FORWARD_REQUEST_HEADERS) {
    const value = incoming.get(name);
    if (value) out.set(name, value);
  }
  // Tenant is decided **here**, never by the browser (see module docstring).
  out.set("X-Tenant-Id", cfg.tenant);
  if (cfg.apiKey) out.set("X-API-Key", cfg.apiKey);
  return out;
}
