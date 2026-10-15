import { defineConfig, devices } from "@playwright/test";

import {
  BASE_URL,
  MOCK_PORT,
  SERVER_TENANT,
  ALLOWED_TENANT,
  UPSTREAM_KEY,
  WEB_PORT,
} from "./e2e/env";

/**
 * 真浏览器 E2E：devmock（上游）+ `next dev`（含服务端代理），Playwright 驱动 Chromium。
 *
 * 为什么值得单独一条腿
 * --------------------
 * `web/tests` 的 jsdom 单测证明的是**组件逻辑**：给 msw 喂响应、断言渲染。
 * 它们证明不了"真浏览器里点下去能跑通"——服务端代理（`app/api/dd/*`）根本没被执行、
 * 租户 cookie 不会被浏览器发出、异步轮询是 mock 出来的、新面板没经过真实 hydration。
 * 这条腿补的正是这一段：**浏览器 → Next 代理 → 上游** 的整条链。
 *
 * 覆盖（每一条都对应 jsdom 测不到的东西）
 * --------------------------------------
 *  1. 服务端代理：浏览器同源打 `/api/dd/*`，密钥由服务端附加（`/v1/whoami` 回显布尔）；
 *  2. 租户 cookie：`DD_WEB_TENANTS` 允许清单内的 cookie 真的改变上游 `X-Tenant-Id`，
 *     清单外 403 失败关闭；
 *  3. 异步轮询：devmock `ASYNC=1` 时 POST 回 202，浏览器真的走"提交→轮询→取结果"；
 *  4. refs 面板：「解析证据引用」**懒解析**（不点不请求），点了解析并展开正文。
 *
 * 边界（如实写）
 * --------------
 *  - 这里跑的是 `next dev`，不是生产产物：证明的是链路与交互，不覆盖构建期差异
 *    （生产版 `node server.js` 的实测记录仍留在 docs/delivery.md §6）；
 *  - 上游是 devmock（`web/devmock/mock-backend.mjs`），不是真实 data_dog 服务：
 *    它按真实端点的**形状**作答，但语义是自己写的；
 *  - "取消调查"仍只有单测覆盖：devmock 的 job 很快就 succeeded，浏览器里点取消是竞态。
 */
export default defineConfig({
  testDir: "./e2e",
  // 一个 devmock 实例 + 内存 job 台账是共享状态，串行最省心也最好读日志。
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  // dev 模式首次访问要按需编译，给宽一点。
  timeout: 90_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: BASE_URL,
    locale: "zh-CN",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    navigationTimeout: 60_000,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      // 上游：devmock。ASYNC=1 → 调查走 202 + 轮询，覆盖客户端异步链路。
      command: "node devmock/mock-backend.mjs",
      env: { PORT: String(MOCK_PORT), ASYNC: "1" },
      url: `http://127.0.0.1:${MOCK_PORT}/healthz`,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
      stdout: "pipe",
    },
    {
      command: `npx next dev -p ${WEB_PORT}`,
      env: {
        // 独立构建目录：本地可能正有另一个 dev server 占着 .next（next.config.ts
        // 的 NEXT_DIST_DIR 注释就是为这个留的），别让两个进程抢同一份编译缓存。
        NEXT_DIST_DIR: ".next-e2e",
        // 走同源代理。必须显式给：仓库里可能有 .env.local 指向直连后端的旧写法，
        // 那会绕过服务端代理——而代理正是这条腿要测的东西。
        NEXT_PUBLIC_API_BASE_URL: "/api/dd",
        // 轮询间隔压到 150ms，让"提交→轮询→取结果"在秒级内走完。
        NEXT_PUBLIC_INVESTIGATE_POLL_MS: "150",
        // 服务端（浏览器看不到）：
        DD_WEB_API_BASE_URL: `http://127.0.0.1:${MOCK_PORT}`,
        DD_WEB_TENANT: SERVER_TENANT,
        // 允许清单：浏览器可以用 dd_web_tenant cookie 在**这两个**里选，别的 403。
        DD_WEB_TENANTS: `${SERVER_TENANT},${ALLOWED_TENANT}`,
        // 非空密钥：用来证明它只出现在服务端，不进浏览器。
        DD_WEB_API_KEY: UPSTREAM_KEY,
        // 本机 E2E 显式放行匿名（配了密钥又没配 Basic 时代理默认失败关闭）。
        DD_WEB_ALLOW_ANONYMOUS: "1",
      },
      url: BASE_URL,
      reuseExistingServer: !process.env.CI,
      timeout: 180_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
});
