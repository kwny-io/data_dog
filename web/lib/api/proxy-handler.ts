/**
 * `/api/dd/*` — the console's server-side proxy to the data_dog service.
 *
 * The browser holds **no** credentials; this handler attaches them server-side.
 * See `lib/api/proxy.ts` for why (Next.js inlines `NEXT_PUBLIC_*` into the
 * client bundle) and for the config/auth rules.
 *
 * The body lives here (plain `Request` → `Response`, no Next runtime types) so
 * it is testable without booting Next; `app/api/dd/[...path]/route.ts` is a thin
 * adapter.
 */
import {
  buildUpstreamHeaders,
  buildUpstreamUrl,
  checkConsoleAuth,
  resolveIdentity,
  resolveProxyConfig,
  sanitizeApiPath,
  type ProxyConfig,
} from "./proxy";

/** Response headers worth passing back to the browser. */
const FORWARD_RESPONSE_HEADERS = ["content-type", "x-request-id", "traceparent"];

function errorJson(
  status: number,
  error: string,
  detail: string,
  extra?: Record<string, string>,
): Response {
  return new Response(JSON.stringify({ ok: false, error, detail }), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...extra },
  });
}

export async function proxyRequest(
  req: Request,
  path: string[],
  env: Record<string, string | undefined> = process.env,
): Promise<Response> {
  const cfg: ProxyConfig = resolveProxyConfig(env);

  const auth = checkConsoleAuth(req.headers.get("authorization"), cfg);
  if (!auth.ok) {
    return errorJson(auth.status, "denied", auth.message, auth.headers);
  }

  // Identity/tenant: server config by default; opt-in allow-list for tenant
  // selection, opt-in trusted-proxy identity for per-user authorization.
  // Anything outside the configured sets is a 403 (fail closed), never a
  // silent fallback.
  const who = resolveIdentity(req.headers, cfg);
  if (!who.ok) {
    return errorJson(who.status, "denied", who.message);
  }

  const apiPath = sanitizeApiPath(path);
  if (!apiPath) {
    // 404 rather than 403: an unknown path genuinely has no route, and the
    // console should not describe the service's surface.
    return errorJson(404, "not_found", "该路径不在此控制台可访问的 API 范围内");
  }

  const target = buildUpstreamUrl(cfg, apiPath, new URL(req.url).search);
  const method = req.method.toUpperCase();
  const hasBody = method !== "GET" && method !== "HEAD";

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method,
      headers: buildUpstreamHeaders(req.headers, cfg, who.tenant),
      body: hasBody ? await req.arrayBuffer() : undefined,
      // Do not follow redirects implicitly: the browser sees the 3xx and the
      // service's own contract stays visible (same stance as the Python side).
      redirect: "manual",
      cache: "no-store",
    });
  } catch (err) {
    const detail = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
    return errorJson(503, "unavailable", `后端不可达：${detail}`);
  }

  const payload = await upstream.arrayBuffer();
  const headers = new Headers();
  for (const name of FORWARD_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  headers.set("Cache-Control", "no-store");
  return new Response(payload, { status: upstream.status, headers });
}
