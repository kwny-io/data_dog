import { afterEach, describe, expect, it } from "vitest";

import { siteOrigin } from "@/lib/site";

/**
 * The site origin is what canonical / og:url are built from. Getting it wrong
 * is the classic "share card points at the wrong domain" launch bug, so the
 * resolution order is pinned here rather than trusted to a manual check.
 *
 * siteOrigin() reads process.env on every call (no module-level cache), so we
 * can flip the variables between cases.
 */

const KEYS = ["NEXT_PUBLIC_SITE_URL", "DD_DOMAIN"] as const;

function clearEnv() {
  for (const k of KEYS) delete process.env[k];
}

afterEach(clearEnv);

describe("siteOrigin", () => {
  it("falls back to the production domain when nothing is set", () => {
    clearEnv();
    expect(siteOrigin()).toBe("https://data.rob.com");
  });

  it("adds an https scheme to a bare DD_DOMAIN host", () => {
    clearEnv();
    process.env.DD_DOMAIN = "dog.example.com";
    expect(siteOrigin()).toBe("https://dog.example.com");
  });

  it("keeps an explicit scheme when one is present", () => {
    clearEnv();
    process.env.DD_DOMAIN = "http://dog.local:3000";
    expect(siteOrigin()).toBe("http://dog.local:3000");
  });

  it("trims a trailing slash so paths never double up", () => {
    clearEnv();
    process.env.DD_DOMAIN = "dog.example.com/";
    expect(siteOrigin()).toBe("https://dog.example.com");
  });

  it("prefers NEXT_PUBLIC_SITE_URL over DD_DOMAIN", () => {
    process.env.DD_DOMAIN = "prod.example.com";
    process.env.NEXT_PUBLIC_SITE_URL = "https://staging.example.org";
    expect(siteOrigin()).toBe("https://staging.example.org");
  });

  it("ignores an empty override and uses the next source", () => {
    process.env.NEXT_PUBLIC_SITE_URL = "";
    process.env.DD_DOMAIN = "prod.example.com";
    expect(siteOrigin()).toBe("https://prod.example.com");
  });
});
