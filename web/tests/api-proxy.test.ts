// @vitest-environment node
//
// 控制台服务端代理：**浏览器不持有凭据**这条安全属性的回归测试。
//
// 背景（真被修掉的漏洞）：前端曾用 `NEXT_PUBLIC_API_KEY` 并在浏览器里设
// `X-API-Key`。Next.js 会把 `NEXT_PUBLIC_*` **内联进客户端包**，所以任何人打开
// devtools 就能读到一把可用的密钥；叠加后端"单密钥 = 全租户"（README 诚实边界），
// 泄露即租户冒充。现在浏览器只请求同源 `/api/dd/*`，密钥与租户由服务端附加。
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  buildUpstreamHeaders,
  buildUpstreamUrl,
  checkConsoleAuth,
  parseTenants,
  resolveProxyConfig,
  resolveTenant,
  safeEqual,
  TENANT_COOKIE,
  sanitizeApiPath,
} from "@/lib/api/proxy";
import { proxyRequest } from "@/lib/api/proxy-handler";

const BASE_ENV = {
  DD_WEB_API_BASE_URL: "http://service:8000",
  DD_WEB_API_KEY: "server-only-key",
  DD_WEB_TENANT: "tenant-from-server",
  DD_WEB_BASIC_AUTH: "",
  DD_WEB_ALLOW_ANONYMOUS: "",
} as Record<string, string | undefined>;

/** 端到端用例用这一份：显式放行匿名（控制台自身的门另有专门用例；
 *  不放开的话，`DD_WEB_API_KEY` 有值而没配认证 → 门会先返回 503，根本走不到转发）。 */
const E2E_ENV = { ...BASE_ENV, DD_WEB_ALLOW_ANONYMOUS: "1" } as Record<string, string | undefined>;

afterEach(() => {
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------- 路径白名单

describe("sanitizeApiPath — 代理不是开放中继", () => {
  it("放行 API 面（v1/* 与三个探针/指标端点）", () => {
    expect(sanitizeApiPath(["v1", "candidates"])).toBe("v1/candidates");
    expect(sanitizeApiPath(["v1", "candidates", "cand_1", "evidence"])).toBe(
      "v1/candidates/cand_1/evidence",
    );
    expect(sanitizeApiPath(["metrics"])).toBe("metrics");
    expect(sanitizeApiPath(["healthz"])).toBe("healthz");
    expect(sanitizeApiPath(["readyz"])).toBe("readyz");
  });

  it("拒绝路径穿越与畸形段", () => {
    expect(sanitizeApiPath(["v1", "..", "..", "etc", "passwd"])).toBeNull();
    expect(sanitizeApiPath(["v1", "a..b"])).toBeNull();
    expect(sanitizeApiPath(["v1", "a\\b"])).toBeNull();
    expect(sanitizeApiPath(["v1", "a?b=1"])).toBeNull();
    expect(sanitizeApiPath(["v1", "a#b"])).toBeNull();
    expect(sanitizeApiPath(["v1", ""])).toBeNull();
    expect(sanitizeApiPath([])).toBeNull();
    expect(sanitizeApiPath(undefined)).toBeNull();
  });

  it("拒绝 API 面之外的根路径（不能拿它探测别的服务）", () => {
    expect(sanitizeApiPath(["admin"])).toBeNull();
    expect(sanitizeApiPath(["v1"])).toBeNull(); // 必须是 v1/<something>
    expect(sanitizeApiPath(["..", "v1", "candidates"])).toBeNull();
  });
});

// ---------------------------------------------------------------- 配置与鉴权

describe("控制台自身的门（失败关闭）", () => {
  it("配了后端密钥但没配用户认证 → **拒绝服务**（不许把特权控制台裸露）", () => {
    const res = checkConsoleAuth(null, resolveProxyConfig(BASE_ENV));
    expect(res.ok).toBe(false);
    if (!res.ok) {
      expect(res.status).toBe(503);
      expect(res.message).toContain("DD_WEB_BASIC_AUTH");
    }
  });

  it("显式 DD_WEB_ALLOW_ANONYMOUS=1 才放行（这是可审计的选择）", () => {
    const cfg = resolveProxyConfig({ ...BASE_ENV, DD_WEB_ALLOW_ANONYMOUS: "1" });
    expect(checkConsoleAuth(null, cfg).ok).toBe(true);
  });

  it("配了 Basic → 必须带对；错/缺都是 401 且带 WWW-Authenticate", () => {
    const cfg = resolveProxyConfig({ ...BASE_ENV, DD_WEB_BASIC_AUTH: "ops:s3cret" });
    const good = `Basic ${Buffer.from("ops:s3cret").toString("base64")}`;
    expect(checkConsoleAuth(good, cfg).ok).toBe(true);

    for (const bad of [null, "Basic nope", `Basic ${Buffer.from("ops:wrong").toString("base64")}`]) {
      const res = checkConsoleAuth(bad, cfg);
      expect(res.ok).toBe(false);
      if (!res.ok) {
        expect(res.status).toBe(401);
        expect(res.headers?.["WWW-Authenticate"]).toContain("Basic");
      }
    }
  });

  it("没配后端密钥（本机开发）→ 不拦（后端自己也不校验）", () => {
    const cfg = resolveProxyConfig({ ...BASE_ENV, DD_WEB_API_KEY: "" });
    expect(checkConsoleAuth(null, cfg).ok).toBe(true);
  });

  it("safeEqual 不同长度/不同内容都判否", () => {
    expect(safeEqual("abc", "abc")).toBe(true);
    expect(safeEqual("abc", "abd")).toBe(false);
    expect(safeEqual("abc", "abcd")).toBe(false);
  });
});

// ---------------------------------------------------------------- 请求头注入

describe("转发头：租户与密钥只由服务端决定", () => {
  it("注入服务端密钥与租户，并**丢弃**浏览器自带的 X-Tenant-Id / X-API-Key", () => {
    const incoming = new Headers({
      "X-Tenant-Id": "victim-tenant",           // 冒充尝试
      "X-API-Key": "stolen-key",                // 冒充尝试
      "Content-Type": "application/json",
      "X-Request-Id": "rid-1",
      "X-Device-Sn": "go2-0001",
      Authorization: "Basic whatever",          // 控制台自己的凭据，不该外传
      Cookie: "session=abc",
    });
    const out = buildUpstreamHeaders(incoming, resolveProxyConfig(BASE_ENV));

    expect(out.get("X-Tenant-Id")).toBe("tenant-from-server");
    expect(out.get("X-API-Key")).toBe("server-only-key");
    expect(out.get("X-Request-Id")).toBe("rid-1");
    expect(out.get("X-Device-Sn")).toBe("go2-0001");
    expect(out.get("Authorization")).toBeNull();
    expect(out.get("Cookie")).toBeNull();
  });

  it("未配密钥时不注入 X-API-Key（后端也没开校验）", () => {
    const cfg = resolveProxyConfig({ ...BASE_ENV, DD_WEB_API_KEY: "" });
    const out = buildUpstreamHeaders(new Headers(), cfg);
    expect(out.get("X-API-Key")).toBeNull();
    expect(out.get("X-Tenant-Id")).toBe("tenant-from-server");
  });

  it("buildUpstreamUrl 保留查询串", () => {
    const cfg = resolveProxyConfig(BASE_ENV);
    expect(buildUpstreamUrl(cfg, "v1/candidates", "?limit=20&offset=0")).toBe(
      "http://service:8000/v1/candidates?limit=20&offset=0",
    );
    expect(buildUpstreamUrl(cfg, "metrics")).toBe("http://service:8000/metrics");
  });
});

// ---------------------------------------------------------------- 端到端（handler）

describe("proxyRequest — 端到端", () => {
  it("把方法/正文/查询串转给后端，并回传状态与正文", async () => {
    const calls: { url: string; init: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        calls.push({ url, init });
        return new Response(JSON.stringify({ ok: true, count: 7 }), {
          status: 200,
          headers: { "Content-Type": "application/json", "X-Request-Id": "rid-up" },
        });
      }),
    );

    const req = new Request("http://console.local/api/dd/v1/candidates?limit=20", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Tenant-Id": "victim" },
      body: JSON.stringify({ events: [] }),
    });
    const res = await proxyRequest(req, ["v1", "candidates"], E2E_ENV);

    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, count: 7 });
    expect(res.headers.get("X-Request-Id")).toBe("rid-up");
    expect(calls[0]!.url).toBe("http://service:8000/v1/candidates?limit=20");
    const sent = calls[0]!.init.headers as Headers;
    expect(sent.get("X-Tenant-Id")).toBe("tenant-from-server");
    expect(sent.get("X-API-Key")).toBe("server-only-key");
    expect(calls[0]!.init.body).toBeDefined();
  });

  it("认证不过时**不发**上游请求", async () => {
    const spy = vi.fn();
    vi.stubGlobal("fetch", spy);
    const req = new Request("http://console.local/api/dd/v1/candidates");
    // BASE_ENV：配了后端密钥、没配用户认证、也没显式放行 → 门在转发之前就拒
    const res = await proxyRequest(req, ["v1", "candidates"], BASE_ENV);
    expect(res.status).toBe(503);
    expect((await res.json()).error).toBe("denied");
    expect(spy).not.toHaveBeenCalled();
  });

  it("越界路径 → 404，且不发上游请求", async () => {
    const spy = vi.fn();
    vi.stubGlobal("fetch", spy);
    const req = new Request("http://console.local/api/dd/etc/passwd");
    const res = await proxyRequest(req, ["etc", "passwd"], {
      ...BASE_ENV,
      DD_WEB_ALLOW_ANONYMOUS: "1",
    });
    expect(res.status).toBe(404);
    expect(spy).not.toHaveBeenCalled();
  });

  it("后端不可达 → 503 unavailable（不让异常冒到浏览器）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("fetch failed");
      }),
    );
    const req = new Request("http://console.local/api/dd/healthz");
    const res = await proxyRequest(req, ["healthz"], E2E_ENV);
    expect(res.status).toBe(503);
    expect((await res.json()).error).toBe("unavailable");
  });

  it("回传后端的非 2xx（不吞不改）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ ok: false, error: "not_found" }), { status: 404 }),
      ),
    );
    const req = new Request("http://console.local/api/dd/v1/candidates/cand_x");
    const res = await proxyRequest(req, ["v1", "candidates", "cand_x"], E2E_ENV);
    expect(res.status).toBe(404);
    expect((await res.json()).error).toBe("not_found");
  });
});

// ------------------------------------------------- 多租户"选择"允许清单（A10）

describe("proxy — 租户选择允许清单（A10）", () => {
  const ENV = { ...BASE_ENV, DD_WEB_TENANTS: "tenant-a, tenant-b" } as Record<
    string,
    string | undefined
  >;

  it("未配 DD_WEB_TENANTS：cookie 被完全忽略（单租户默认）", () => {
    const req = new Headers({ cookie: `${TENANT_COOKIE}=tenant-b` });
    const out = buildUpstreamHeaders(req, resolveProxyConfig(BASE_ENV));
    expect(out.get("X-Tenant-Id")).toBe("tenant-from-server");
  });

  it("配了允许清单：cookie 可在清单内选租户", () => {
    const req = new Headers({ cookie: `x=1; ${TENANT_COOKIE}=tenant-b` });
    const cfg = resolveProxyConfig(ENV);
    const who = resolveTenant(req, cfg);
    expect(who).toEqual({ ok: true, tenant: "tenant-b" });
    const out = buildUpstreamHeaders(req, cfg, who.ok ? who.tenant : "");
    expect(out.get("X-Tenant-Id")).toBe("tenant-b");
  });

  it("清单外的租户 → 403（失败关闭，不回退到默认租户）", () => {
    const req = new Headers({ cookie: `${TENANT_COOKIE}=victim` });
    const who = resolveTenant(req, resolveProxyConfig(ENV));
    expect(who.ok).toBe(false);
    if (!who.ok) expect(who.status).toBe(403);
  });

  it("即便配了清单，浏览器自带的 X-Tenant-Id 头仍被忽略（只认服务端清单）", () => {
    const req = new Headers({
      "X-Tenant-Id": "victim",
      cookie: `${TENANT_COOKIE}=tenant-a`,
    });
    expect(resolveTenant(req, resolveProxyConfig(ENV))).toEqual({
      ok: true,
      tenant: "tenant-a",
    });
  });

  it("清单外的租户在**转发之前**就被拒（不发上游请求）", async () => {
    const spy = vi.fn();
    vi.stubGlobal("fetch", spy);
    const req = new Request("http://console.local/api/dd/v1/candidates", {
      headers: { cookie: `${TENANT_COOKIE}=victim` },
    });
    const res = await proxyRequest(req, ["v1", "candidates"], {
      ...ENV,
      DD_WEB_ALLOW_ANONYMOUS: "1",
    });
    expect(res.status).toBe(403);
    expect(spy).not.toHaveBeenCalled();
  });

  it("DD_WEB_TENANTS 解析：去空、去重、保序", () => {
    expect(parseTenants(" a , b ,, a ")).toEqual(["a", "b"]);
    expect(parseTenants("")).toEqual([]);
    expect(parseTenants(undefined)).toEqual([]);
  });
});
