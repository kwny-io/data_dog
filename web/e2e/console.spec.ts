import { expect, test, type Page } from "@playwright/test";

import {
  ALLOWED_TENANT,
  BASE_URL,
  DISALLOWED_TENANT,
  SERVER_TENANT,
  TENANT_COOKIE,
  UPSTREAM_KEY,
} from "./env";

/**
 * 控制台外壳：**服务端代理** 与 **凭据不进浏览器** 的真浏览器证明。
 *
 * jsdom 单测里 `app/api/dd/[...path]/route.ts` 从未被执行——它们用 msw 直接喂响应。
 * 这组测试才真的把"浏览器 → 同源 /api/dd → 服务端代理 → 上游"走了一遍。
 */

/** devmock 的"我是谁"探针（真实后端没有这个端点，见 devmock/mock-backend.mjs）。 */
type WhoAmI = {
  status: number;
  body: {
    ok?: boolean;
    tenant?: string | null;
    api_key_present?: boolean;
    cookie_forwarded?: boolean;
    error?: string;
  };
};

/** 用**页面自己的** fetch 打代理，从而带上浏览器的 cookie jar（这才是真实路径）。 */
async function whoami(page: Page): Promise<WhoAmI> {
  return page.evaluate(async () => {
    const res = await fetch("/api/dd/v1/whoami");
    return { status: res.status, body: await res.json() };
  });
}

/**
 * 租户徽标的**稳定钩子**（`data-testid`）。
 *
 * 以前这里是一整句中文 title 文案，于是"改文案"= "测试红"：本轮把徽标从
 * 无条件"浏览器无法切换"改成按模式如实说明（单租户 / DD_WEB_TENANTS 白名单），
 * 两处断言当场找不到元素。定位器绑文案是错的——**用 testid 定位，用文案断言**。
 */
const TENANT_BADGE = "tenant-badge";

test.describe("控制台外壳：服务端代理 / 凭据 / 租户", () => {
  test("页面加载后 /readyz 经服务端代理返回「就绪」，租户徽标显示服务端租户", async ({
    page,
  }) => {
    await page.goto("/");
    // HealthBadge 的 title 由 /readyz 响应决定；它出现即证明代理链路真的通了
    // （浏览器不带任何凭据，密钥在服务端被加上）。
    await expect(page.getByTitle(/服务就绪/)).toBeVisible();
    await expect(page.getByTestId(TENANT_BADGE)).toContainText(SERVER_TENANT);
  });

  test("上游密钥只由服务端附加：浏览器请求不带它，客户端脚本里也没有它", async ({
    page,
  }) => {
    const withApiKey: string[] = [];
    page.on("request", (req) => {
      if (req.url().includes("/api/dd/") && req.headers()["x-api-key"])
        withApiKey.push(req.url());
    });

    await page.goto("/");
    await expect(page.getByTitle(/服务就绪/)).toBeVisible();

    // 服务端确实把密钥加上去了（探针只回布尔，永不回密钥本身）。
    const info = await whoami(page);
    expect(info.body.api_key_present).toBe(true);
    // 并且没有把浏览器 cookie 原样透传上游——代理的请求头是允许清单，不是转发。
    expect(info.body.cookie_forwarded).toBe(false);

    // 浏览器自己发出的请求里没有任何凭据。
    expect(withApiKey).toEqual([]);

    // 客户端脚本里也不该有它：Next 会把 NEXT_PUBLIC_* 内联进包，
    // 凭据一旦走那条路，谁打开 devtools 都能读到。这条断言就是那个护栏。
    const scriptSrcs = await page.evaluate(() =>
      Array.from(document.querySelectorAll<HTMLScriptElement>("script[src]"))
        .map((el) => el.src)
        .filter((s) => s.startsWith(window.location.origin)),
    );
    expect(scriptSrcs.length).toBeGreaterThan(0);
    for (const src of scriptSrcs) {
      const res = await page.request.get(src);
      expect(res.status()).toBe(200);
      expect(await res.text()).not.toContain(UPSTREAM_KEY);
    }
  });

  test("租户 cookie 只在 DD_WEB_TENANTS 允许清单内生效，清单外 403 失败关闭", async ({
    page,
    context,
  }) => {
    await page.goto("/");
    await expect(page.getByTitle(/服务就绪/)).toBeVisible();

    // 默认：服务端租户。
    expect((await whoami(page)).body.tenant).toBe(SERVER_TENANT);

    // 清单内的 cookie → 上游真的换了 X-Tenant-Id。这条只有在真浏览器里成立：
    // cookie 由浏览器发出、由代理读取，jsdom 测不到这一段。
    await context.addCookies([
      { name: TENANT_COOKIE, value: ALLOWED_TENANT, url: BASE_URL },
    ]);
    expect((await whoami(page)).body.tenant).toBe(ALLOWED_TENANT);

    // 徽标仍显示服务端租户：它是**只读展示**，不跟随 cookie。这是当前实现的事实，
    // 这里如实断言它（多租户登录属后端开放问题，不在本次范围）。
    await expect(page.getByTestId(TENANT_BADGE)).toContainText(SERVER_TENANT);

    // 清单外 → 403，失败关闭（不是静默回落到默认租户）。
    await context.addCookies([
      { name: TENANT_COOKIE, value: DISALLOWED_TENANT, url: BASE_URL },
    ]);
    const denied = await whoami(page);
    expect(denied.status).toBe(403);
    expect(denied.body.error).toBe("denied");
  });

  test("代理不是开放中继：非白名单路径一律 404", async ({ page }) => {
    await page.goto("/");
    for (const path of ["/api/dd/etc/passwd", "/api/dd/"]) {
      const res = await page.request.get(path);
      expect(res.status(), path).toBe(404);
    }
  });
});
