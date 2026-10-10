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
    // Node's fetch needs an absolute URL; the app itself uses the **relative**
    // console proxy path (`/api/dd`) so the browser never holds credentials.
    // Tests pin a host so msw can intercept.
    env: {
      NEXT_PUBLIC_API_BASE_URL: "http://localhost:3000/api/dd",
    },
  },
});
