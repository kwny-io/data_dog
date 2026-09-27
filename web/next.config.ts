import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // standalone output keeps the Docker image small (multi-stage build in Dockerfile)
  output: "standalone",
  poweredByHeader: false,
  eslint: { ignoreDuringBuilds: false },
  typedRoutes: true,
};

export default config;
