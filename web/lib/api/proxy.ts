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
 *  - **Single-tenant by default (architecture review A10).** A real multi-tenant
 *    console needs an identity layer (who may see which tenant) that this repo
 *    does not have. The one opt-in step here is `DD_WEB_TENANTS`: a server-side
 *    **allow-list** plus a `dd_web_tenant` cookie. With it set, the browser may
 *    pick **from that list only** (anything else → 403); with it unset the cookie
 *    is ignored and behavior is byte-identical to before. It is tenant
 *    *selection*, not user authorization: run it behind an authenticating proxy.
 *  - **Per-user authorization is opt-in** (`DD_WEB_TRUST_PROXY_AUTH=1` +
 *    `DD_WEB_USER_TENANTS`): a trusted reverse proxy names the user, and the
 *    console refuses tenants outside that user list. Console-level only — the
 *    service still enforces tenancy itself from the forced `X-Tenant-Id`.
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
  /** Opt-in allow-list (`DD_WEB_TENANTS`, comma separated) for tenant *selection*.
   *  Empty = single-tenant mode: the `dd_web_tenant` cookie is ignored outright. */
  tenants: string[];
  /** Opt-in trusted-proxy identity (`DD_WEB_TRUST_PROXY_AUTH=1`). Off = nobody is
   *  identified and the console behaves exactly as before. The upstream proxy
   *  MUST strip/overwrite `userHeader`, or anyone can impersonate a user. */
  trustProxyAuth: boolean;
  /** Header carrying the authenticated user (default `x-auth-request-user`). */
  userHeader: string;
  /** `DD_WEB_USER_TENANTS="alice:tenant-a,tenant-b;bob:tenant-b"` — user → allowed
   *  tenants. With identity on, a request may only touch tenants assigned to it. */
  userTenants: Map<string, string[]>;
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

/** `DD_WEB_TENANTS="a, b"` → `["a", "b"]`（去空、去重、保序）。空 = 单租户模式。 */
export function parseTenants(raw: string | undefined): string[] {
  const out: string[] = [];
  for (const part of (raw ?? "").split(",")) {
    const t = part.trim();
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}


/** `alice:a,b;bob:b` → Map。空/坏片段跳过；用户重复时**取并集**（不静默丢失）。 */
export function parseUserTenants(raw: string | undefined): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const entry of (raw ?? "").split(";")) {
    const trimmed = entry.trim();
    if (!trimmed) continue;
    const idx = trimmed.indexOf(":");
    if (idx <= 0) continue;
    const user = trimmed.slice(0, idx).trim();
    const tenants = parseTenants(trimmed.slice(idx + 1));
    if (!user || tenants.length === 0) continue;
    const prev = out.get(user) ?? [];
    out.set(user, [...prev, ...tenants.filter((t) => !prev.includes(t))]);
  }
  return out;
}


export function resolveProxyConfig(
  env: Record<string, string | undefined> = process.env,
): ProxyConfig {
  return {
    baseUrl: (env.DD_WEB_API_BASE_URL ?? DEFAULT_BASE_URL).replace(/\/+$/, ""),
    apiKey: (env.DD_WEB_API_KEY ?? "").trim(),
    tenant: (env.DD_WEB_TENANT ?? DEFAULT_TENANT).trim() || DEFAULT_TENANT,
    tenants: parseTenants(env.DD_WEB_TENANTS),
    trustProxyAuth: (env.DD_WEB_TRUST_PROXY_AUTH ?? "").trim() === "1",
    userHeader: ((env.DD_WEB_USER_HEADER ?? "").trim() || "x-auth-request-user").toLowerCase(),
    userTenants: parseUserTenants(env.DD_WEB_USER_TENANTS),
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

/** Cookie the browser may use to *select* (not authorize) a tenant. */
export const TENANT_COOKIE = "dd_web_tenant";

function readCookie(header: string | null, name: string): string {
  if (!header) return "";
  for (const part of header.split(";")) {
    const eq = part.indexOf("=");
    if (eq < 0) continue;
    if (part.slice(0, eq).trim() !== name) continue;
    const value = part.slice(eq + 1).trim();
    try {
      return decodeURIComponent(value);
    } catch {
      return value;      // 坏编码不当攻击，按原值比对（比对不上自然被拒）
    }
  }
  return "";
}

/**
 * Which tenant this request runs as.
 *
 * Default (no `DD_WEB_TENANTS`): always the server tenant — the browser never gets
 * to choose. With an allow-list: a `dd_web_tenant` cookie may pick **from the
 * list**; anything outside it is a hard 403 (fail closed, and say why).
 */
export function resolveTenant(
  incoming: Headers,
  cfg: ProxyConfig,
): { ok: true; tenant: string } | { ok: false; status: number; message: string } {
  if (cfg.tenants.length === 0) return { ok: true, tenant: cfg.tenant };
  const chosen = readCookie(incoming.get("cookie"), TENANT_COOKIE);
  if (!chosen) return { ok: true, tenant: cfg.tenant };
  if (!cfg.tenants.includes(chosen)) {
    return {
      ok: false,
      status: 403,
      message:
        `租户 ${chosen} 不在 DD_WEB_TENANTS 允许清单内：控制台只允许在服务端配置的租户` +
        "清单里选择，不允许任意租户（这是选择，不是授权）。",
    };
  }
  return { ok: true, tenant: chosen };
}


/**
 * Who is asking, and which tenant they run as.
 *
 * Two independent **opt-in** layers (both off by default → behavior unchanged):
 *  - `DD_WEB_TENANTS`: the browser may *select* a tenant from a server allow-list
 *    (`resolveTenant`);
 *  - `DD_WEB_TRUST_PROXY_AUTH=1` + `DD_WEB_USER_TENANTS`: a trusted reverse proxy
 *    says who the user is (`userHeader`) and the console enforces that the user
 *    only touches tenants assigned to them. This is **console-level** authorization.
 *
 * Boundaries (read before enabling): the upstream proxy MUST strip the user header
 * from inbound requests, or anyone can impersonate a user; the backend still sees
 * one global key and does its own tenant enforcement from `X-Tenant-Id`; this is
 * not a user directory, not OIDC, and not per-user authorization *inside* the
 * service. See docs/delivery.md for what remains open.
 */
export function resolveIdentity(
  incoming: Headers,
  cfg: ProxyConfig,
): { ok: true; user: string; tenant: string } | { ok: false; status: number; message: string } {
  if (!cfg.trustProxyAuth) {
    const base = resolveTenant(incoming, cfg);
    return base.ok ? { ok: true, user: "", tenant: base.tenant } : base;
  }

  const user = (incoming.get(cfg.userHeader) ?? "").trim();
  if (!user) {
    return { ok: false, status: 403,
             message: `缺少身份头 ${cfg.userHeader}：反代没有注入已认证用户（失败关闭）` };
  }
  const own = cfg.userTenants.get(user);
  if (!own || own.length === 0) {
    return { ok: false, status: 403,
             message: `未知用户 ${user}：不在 DD_WEB_USER_TENANTS 里（失败关闭，不给默认权限）` };
  }

  const cookie = readCookie(incoming.get("cookie"), TENANT_COOKIE);
  if (cookie && !own.includes(cookie)) {
    return { ok: false, status: 403,
             message: `用户 ${user} 无权访问租户 ${cookie}（不在其授权清单内）` };
  }
  const tenant = cookie || (own.includes(cfg.tenant) ? cfg.tenant : own[0]!);
  if (cfg.tenants.length > 0 && !cfg.tenants.includes(tenant)) {
    return { ok: false, status: 403,
             message: `用户 ${user} 的租户 ${tenant} 不在 DD_WEB_TENANTS 允许清单内` };
  }
  return { ok: true, user, tenant };
}


export function buildUpstreamHeaders(
  incoming: Headers,
  cfg: ProxyConfig,
  tenant: string = cfg.tenant,
): Headers {
  const out = new Headers();
  for (const name of FORWARD_REQUEST_HEADERS) {
    const value = incoming.get(name);
    if (value) out.set(name, value);
  }
  // Tenant is decided **here** by `resolveTenant` (single-tenant by default;
  // with an allow-list the browser may pick from that list only). Never a raw
  // client header — see module docstring.
  out.set("X-Tenant-Id", tenant);
  if (cfg.apiKey) out.set("X-API-Key", cfg.apiKey);
  return out;
}
