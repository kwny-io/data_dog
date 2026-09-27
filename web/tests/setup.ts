import "@testing-library/jest-dom/vitest";
import { afterEach, afterAll, beforeAll, vi } from "vitest";

import { server } from "./msw/server";

// This setup file is shared by jsdom component tests and node-only tests
// (api-client, metrics). Guard every DOM touch so the node files can load it.
const hasDom = typeof document !== "undefined";

beforeAll(async () => {
  server.listen({ onUnhandledRequest: "error" });

  if (!hasDom) return;

  // jsdom lacks these; recharts/ResponsiveContainer and our theme hook touch them.
  const { cleanup } = await import("@testing-library/react");
  void cleanup;

  if (!window.matchMedia) {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });
  }
  if (!globalThis.ResizeObserver) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  }
});

afterEach(async () => {
  server.resetHandlers();
  // The tech-fields store caches its snapshot in module scope so the external
  // store is stable across renders. That cache outlives a single test in the
  // same worker, so reset it here — otherwise one test's toggle would leak.
  const { resetTechFields } = await import("@/lib/tech-fields");
  resetTechFields();
  if (!hasDom) return;
  const { cleanup } = await import("@testing-library/react");
  cleanup();
  window.localStorage.clear();
});

afterAll(() => server.close());
