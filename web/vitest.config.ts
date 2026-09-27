import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL(".", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/**/*.test.{ts,tsx}"],
    css: false,
    // Node's fetch needs an absolute URL. The app uses a relative base in
    // production (same-origin behind Caddy); in tests we pin a host so msw
    // can intercept.
    env: {
      NEXT_PUBLIC_API_BASE_URL: "http://localhost:8000",
      NEXT_PUBLIC_DEFAULT_TENANT: "tenant-test",
    },
  },
});
