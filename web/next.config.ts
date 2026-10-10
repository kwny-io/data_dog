import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // standalone output keeps the Docker image small (multi-stage build in Dockerfile)
  output: "standalone",
  poweredByHeader: false,
  eslint: { ignoreDuringBuilds: false },
  typedRoutes: true,
  // 构建缓存目录。默认 .next；可用 NEXT_DIST_DIR 覆盖，让第二个 dev server
  // （如桌面预览）写独立目录，避免两个进程抢同一份 .next 导致偶发编译错乱。
  // 正常 `npm run dev` / `npm run build` 不设该变量 → 行为完全不变。
  distDir: process.env.NEXT_DIST_DIR ?? ".next",
  // 注意：**没有** `rewrites()` 反代了。以前用 `NEXT_API_PROXY` 把 /v1/* 反代到后端，
  // 但 rewrite **不能注入请求头**，所以密钥只能塞进浏览器（NEXT_PUBLIC_API_KEY）——
  // 那正是被修掉的泄露（Next 会把 NEXT_PUBLIC_* 内联进客户端包）。
  // 现在统一走 `app/api/dd/[...path]/route.ts`：服务端代理，密钥与租户来自
  // **服务端**环境变量（DD_WEB_*），浏览器不持有任何凭据。
};

export default config;
