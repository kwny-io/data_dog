import { describe, expect, it } from "vitest";

import {
  describeRuntimeError,
  effectiveTenant,
  emptyAlertsHint,
  emptyCandidatesHint,
  readCookieValue,
  TENANT_COOKIE_NAME,
} from "@/lib/hints";

/**
 * 空列表有两种含义，界面必须说清是哪一种：
 *  1) 真的没数据；
 *  2) **租户不对**（多租户隔离正常工作）。
 * 本仓库真实踩过第 2 种：`make ingest` 投 `tenant-real`、控制台读 `tenant-demo`，
 * 照 README 跑完演示列表永远为空，被当成"接口坏了"排查。
 * 这里把"必须点名当前租户 + 必须说这不是故障"钉死。
 */
describe("空态文案：必须指名租户并排除误判", () => {
  it("候选空态点出当前租户", () => {
    const hint = emptyCandidatesHint("tenant-demo");
    expect(hint).toContain("tenant-demo");
  });

  it("候选空态点出另一个租户时也如实显示（不是写死的常量）", () => {
    const hint = emptyCandidatesHint("tenant-acme");
    expect(hint).toContain("tenant-acme");
    expect(hint).not.toContain("tenant-demo");
  });

  it("候选空态明确说'不是接口故障'，并给出下一步动作", () => {
    const hint = emptyCandidatesHint("tenant-demo");
    expect(hint).toContain("不是接口故障");
    expect(hint).toContain("make ingest");
  });

  it("告警空态同样点出租户并排除误判", () => {
    const hint = emptyAlertsHint("tenant-demo");
    expect(hint).toContain("tenant-demo");
    expect(hint).toContain("不是接口故障");
  });

  it("有状态筛选时，告警空态改为解释筛选而不是讲租户（避免答非所问）", () => {
    const hint = emptyAlertsHint("tenant-demo", "已确认");
    expect(hint).toContain("已确认");
    expect(hint).toContain("去掉筛选");
    expect(hint).not.toContain("tenant-demo");
  });
});

// ---------------------------------------------------------------- 生效租户
describe("effectiveTenant：界面显示必须与代理实际行为一致", () => {
  it("单租户模式：cookie 被代理忽略 → 显示服务端租户", () => {
    // proxy.ts: tenants.length === 0 时**完全不看** cookie
    expect(effectiveTenant("tenant-demo", [], "tenant-evil")).toBe("tenant-demo");
  });

  it("白名单模式 + cookie 在清单内 → 显示该租户（这才是真实生效的）", () => {
    // 真后端 e2e 抓到的场景：数据走 tenant-alt，旧界面却写 tenant-demo
    expect(effectiveTenant("tenant-demo", ["tenant-alt"], "tenant-alt")).toBe("tenant-alt");
  });

  it("白名单模式 + 无 cookie → 服务端默认", () => {
    expect(effectiveTenant("tenant-demo", ["tenant-alt"], null)).toBe("tenant-demo");
    expect(effectiveTenant("tenant-demo", ["tenant-alt"], "")).toBe("tenant-demo");
  });

  it("白名单模式 + cookie 在清单外 → 回落默认（代理会 403，界面不显示越权值）", () => {
    expect(effectiveTenant("tenant-demo", ["tenant-alt"], "tenant-evil")).toBe("tenant-demo");
  });

  it("cookie 名必须与代理用的那个一致（两处常量漂移就会静默失效）", async () => {
    const proxy = await import("@/lib/api/proxy");
    expect(TENANT_COOKIE_NAME).toBe(proxy.TENANT_COOKIE);
  });
});

describe("readCookieValue", () => {
  it("能从 document.cookie 原始串里取值，且不误匹配同前缀的 cookie", () => {
    const raw = "a=1; dd_web_tenant=tenant-alt; dd_web_tenant_x=nope";
    expect(readCookieValue(raw, TENANT_COOKIE_NAME)).toBe("tenant-alt");
    expect(readCookieValue(raw, "missing")).toBeNull();
    expect(readCookieValue(null, TENANT_COOKIE_NAME)).toBeNull();
  });
});

/**
 * 错误路径也要"说得清"：界面不能用 `[429] rate_limited:` 这种机器格式对操作员说话。
 * 每条分支都必须给出**下一步动作**，而不只是复述状态码。
 */
describe("错误文案：每条路径都有可执行的下一步", () => {
  const cases = [
    { err: { status: 429, code: "rate_limited", message: "[429] rate_limited" }, must: "再试" },
    { err: { status: 503, message: "[503] unavailable" }, must: "/readyz" },
    { err: { status: 500, message: "[500] execution_failed" }, must: "requestId" },
    { err: { status: 401, code: "denied", message: "[401] denied" }, must: "租户" },
    { err: { status: 404, code: "not_found", message: "[404] not_found" }, must: "别的租户" },
    { err: { name: "TimeoutError", message: "请求超时" }, must: "P99" },
    { err: { name: "NetworkError", message: "fetch failed" }, must: "后端" },
  ];

  for (const { err, must } of cases) {
    it(`${err.status ?? err.name} 给出动作（含「${must}」）`, () => {
      const { title, action } = describeRuntimeError(err);
      expect(title.length).toBeGreaterThan(0);
      expect(action).toContain(must);
    });
  }

  it("限流时如果后端给了等待秒数，就说具体秒数", () => {
    const { action } = describeRuntimeError({ status: 429, retryAfterSeconds: 7 });
    expect(action).toContain("7 秒");
  });

  it("限流但没给秒数时不说假话（不编具体数字）", () => {
    const { action } = describeRuntimeError({ status: 429 });
    expect(action).not.toMatch(/\d+ 秒后/);
  });

  it("未知错误保留原始信息，不吞掉线索", () => {
    const { action } = describeRuntimeError({ message: "some weird thing" });
    expect(action).toContain("some weird thing");
  });

  it("null 也不崩（throws 空值）", () => {
    expect(describeRuntimeError(null).title).toContain("未知");
  });
});
