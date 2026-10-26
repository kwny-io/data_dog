import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * 回归：`NEXT_PUBLIC_API_BASE_URL` 为空串时，控制台必须回落到同源代理 `/api/dd`。
 *
 * 这个用例来自一次**实机事故**：`web/Dockerfile` 的 ARG 默认值是 `""`，该值被
 * Next 内联进浏览器包；`?? "/api/dd"` 拦不住空串，于是浏览器把 `/v1/candidates`
 * 打到页面自己的源（3101），404，界面只显示 0、页面仍 200、容器 healthy。
 * 演示时看起来只是“没数据”，实际是控制台与后端彻底断开。
 *
 * 因此断言的是**行为**（最终基路径），不是某个写法：
 *   - ""、纯空白、"//" → /api/dd
 *   - "/api/dd" → /api/dd（已显式给出）
 *   - 绝对地址 → 原样（去掉尾斜杠）
 */
async function baseUrlFor(value: string | undefined): Promise<string> {
  vi.resetModules();
  if (value === undefined) delete process.env.NEXT_PUBLIC_API_BASE_URL;
  else process.env.NEXT_PUBLIC_API_BASE_URL = value;
  const mod = await import("@/lib/api/client");
  return mod.API_BASE_URL;
}

afterEach(() => {
  delete process.env.NEXT_PUBLIC_API_BASE_URL;
  vi.resetModules();
});

describe("API_BASE_URL 的回落规则", () => {
  it("未设置 → 同源代理", async () => {
    expect(await baseUrlFor(undefined)).toBe("/api/dd");
  });

  it("空串 → 同源代理（Dockerfile ARG 的默认值就是空串）", async () => {
    expect(await baseUrlFor("")).toBe("/api/dd");
  });

  it("纯空白 / 只有斜杠 → 同源代理", async () => {
    expect(await baseUrlFor("   ")).toBe("/api/dd");
    expect(await baseUrlFor("/")).toBe("/api/dd");
    expect(await baseUrlFor("//")).toBe("/api/dd");
  });

  it("显式给出路径 → 原样，去掉尾斜杠", async () => {
    expect(await baseUrlFor("/api/dd/")).toBe("/api/dd");
  });

  it("绝对地址 → 原样", async () => {
    expect(await baseUrlFor("https://dog.example.com/api/dd/")).toBe(
      "https://dog.example.com/api/dd",
    );
  });
});
