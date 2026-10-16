import { expect, test } from "@playwright/test";

import {
  ALLOWED_TENANT,
  BASE_URL,
  DISALLOWED_TENANT,
  REAL_BACKEND,
  REAL_BASE_URL,
  SERVER_TENANT,
  TENANT_COOKIE,
  UPSTREAM_KEY,
} from "./env";

/**
 * 真后端腿（`E2E_REAL_BACKEND=1`）：**浏览器 → 服务端代理 → 真实 data_dog 服务**。
 *
 * 为什么还要这一条（默认那条腿不够）
 * ----------------------------------
 * 默认腿的上游是 devmock，它**不校验密钥**。所以它能证明"链路通"，但证明不了
 * "代理把服务端那把密钥正确带给了真实服务"——那正是把密钥藏在服务端的全部意义。
 * 第一次手工把它指向真后端时，带鉴权的接口全 401，一度被误判成产品缺陷；查清后是
 * **验证编排**问题（端口 / env 没对齐），不是代理的 bug。这条腿把那次的结论钉成
 * 一条可重复的门禁，并把两条编排坑写进 docs/delivery.md §6。
 *
 * 跨过的三段：浏览器（无凭据）→ Next 服务端代理（加密钥）→ 真实 FastAPI 服务（校验密钥）。
 *
 * 跑法：`cd web && E2E_REAL_BACKEND=1 npm run e2e`（起真实 Python 服务 + 独立 SQLite）。
 */
test.describe("真后端腿：浏览器 → 服务端代理 → 真实 data_dog 服务", () => {
  test.skip(!REAL_BACKEND, "需 E2E_REAL_BACKEND=1（会起真实 Python 服务）");

  test("鉴权连携：同一把密钥直连可用、经代理可用；不带密钥直连 401", async ({
    page,
    request,
  }) => {
    // 直连真服务：**不带密钥 → 401**。这条断言是"上游真的在校验"的证据；
    // 没有它，一个不做鉴权的假上游也能让下面两条断言全绿。
    const noKey = await request.get(`${REAL_BASE_URL}/v1/summary`, {
      headers: { "X-Tenant-Id": SERVER_TENANT },
    });
    expect(noKey.status()).toBe(401);

    // 带密钥直连 → 200，且服务自报 auth_enabled=true（密钥确实配在它身上）。
    const direct = await request.get(`${REAL_BASE_URL}/v1/summary`, {
      headers: { "X-API-Key": UPSTREAM_KEY, "X-Tenant-Id": SERVER_TENANT },
    });
    expect(direct.status()).toBe(200);
    expect((await direct.json()).auth_enabled).toBe(true);

    // 经服务端代理：浏览器请求里没有任何凭据，密钥由服务端附加 → 200。
    await page.goto("/");
    await expect(page.getByTitle(/服务就绪/)).toBeVisible();
    const viaProxy = await page.request.get("/api/dd/v1/summary");
    expect(viaProxy.status()).toBe(200);
    expect((await viaProxy.json()).auth_enabled).toBe(true);
  });

  test("租户选择对真实后端同样生效：允许清单内 200，清单外 403 失败关闭", async ({
    page,
    context,
  }) => {
    await page.goto("/");
    await expect(page.getByTitle(/服务就绪/)).toBeVisible();

    // 默认（无 cookie）：服务端租户，真服务接受。
    expect((await page.request.get("/api/dd/v1/candidates")).status()).toBe(200);

    // 清单内 cookie：租户换了，真实 FastAPI 服务仍然 200。
    await context.addCookies([
      { name: TENANT_COOKIE, value: ALLOWED_TENANT, url: BASE_URL },
    ]);
    expect((await page.request.get("/api/dd/v1/candidates")).status()).toBe(200);

    // 清单外 cookie：**代理在转发之前**就 403（不是后端回的），失败关闭。
    await context.addCookies([
      { name: TENANT_COOKIE, value: DISALLOWED_TENANT, url: BASE_URL },
    ]);
    const denied = await page.request.get("/api/dd/v1/candidates");
    expect(denied.status()).toBe(403);
    expect((await denied.json()).error).toBe("denied");
  });

  test("确认对端确实是真服务：devmock 专属探针不存在", async ({ page }) => {
    await page.goto("/");
    // /v1/whoami 只有 devmock 有，真实服务没有这个路由 → 经代理 404。
    // 这条是"没有悄悄跑回 devmock"的护栏：真腿与默认腿的端口不同，别配串了。
    expect((await page.request.get("/api/dd/v1/whoami")).status()).toBe(404);
  });
});
