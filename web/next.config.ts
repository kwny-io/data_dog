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
  // 可选：把后端 API 反代到本机 dev server 同源（NEXT_API_PROXY=后端地址时生效）。
  // 这样前端发同源请求、由 Next 转发到后端 → 无需后端 CORS 白名单。
  // 仅在设了该变量时挂载；正常开发/生产（Caddy 同源）不受影响。
  async rewrites() {
    const target = process.env.NEXT_API_PROXY;
    if (!target) return [];
    return [
      { source: "/v1/:path*", destination: `${target}/v1/:path*` },
      { source: "/healthz", destination: `${target}/healthz` },
      { source: "/readyz", destination: `${target}/readyz` },
      { source: "/metrics", destination: `${target}/metrics` },
    ];
  },
};

export default config;
