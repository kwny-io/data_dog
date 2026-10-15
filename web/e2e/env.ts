/**
 * E2E 的共享常量：`playwright.config.ts` 与各规格文件共用一份，避免两处漂移。
 *
 * 端口刻意避开 3000/8000——本地很可能正跑着 `npm run dev` 或真实后端，
 * E2E 不该去踩它们（`reuseExistingServer` 只复用**本套**端口上的服务）。
 */
export const MOCK_PORT = Number(process.env.E2E_MOCK_PORT ?? 8100);
export const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 3100);
export const BASE_URL = `http://127.0.0.1:${WEB_PORT}`;

/**
 * 故意设成非空的上游密钥（`DD_WEB_API_KEY`）。
 *
 * 它的唯一用途是**被断言不存在于浏览器里**：代理把它加在服务端请求上，
 * 客户端包/浏览器请求里出现这个字符串即测试失败。所以这里的字面量必须独一无二。
 */
export const UPSTREAM_KEY = "e2e-upstream-key-must-not-reach-the-browser";

/** 与 `lib/api/proxy.ts::TENANT_COOKIE` 同名的租户选择 cookie。 */
export const TENANT_COOKIE = "dd_web_tenant";

/** 服务端租户与允许清单（与 config 里给 webServer 的 env 一一对应）。 */
export const SERVER_TENANT = "tenant-demo";
export const ALLOWED_TENANT = "tenant-alt";
export const DISALLOWED_TENANT = "tenant-evil";
