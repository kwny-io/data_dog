import { describe, expect, it } from "vitest";

import { trustProxyAuthGuard } from "@/lib/api/proxy";

/**
 * 启动护栏的判定表：把"开了可信反代信任，就必须有门或有显式确认"钉死。
 *
 * 它是纯函数（只读 env），所以判定表可以用单测穷举，不依赖进程状态。
 * `instrumentation.ts` 只是"拿到原因就拒绝启动"的那层薄壳。
 */
const BASE = {
  DD_WEB_API_KEY: "server-only-key",
  DD_WEB_TENANT: "tenant-from-server",
} as Record<string, string | undefined>;

describe("启动护栏 — DD_WEB_TRUST_PROXY_AUTH 失败关闭", () => {
  it("默认（未开信任）：放行", () => {
    expect(trustProxyAuthGuard(BASE)).toBeNull();
  });

  it("开了信任、既无门也无确认：拒绝启动，并点名两条出路", () => {
    const msg = trustProxyAuthGuard({ ...BASE, DD_WEB_TRUST_PROXY_AUTH: "1" });
    expect(msg).toBeTruthy();
    expect(msg).toContain("拒绝启动");
    expect(msg).toContain("DD_WEB_BASIC_AUTH");
    expect(msg).toContain("DD_WEB_TRUST_PROXY_ACK=1");
    // 必须点明"要剥的是哪个头"，否则运维不知道该在反代上配什么。
    expect(msg).toContain("x-auth-request-user");
  });

  it("有 Basic 门：放行（伪造者还得先有口令）", () => {
    expect(
      trustProxyAuthGuard({
        ...BASE,
        DD_WEB_TRUST_PROXY_AUTH: "1",
        DD_WEB_BASIC_AUTH: "u:p",
      }),
    ).toBeNull();
  });

  it("显式确认 DD_WEB_TRUST_PROXY_ACK=1：放行（责任转移给部署者）", () => {
    expect(
      trustProxyAuthGuard({
        ...BASE,
        DD_WEB_TRUST_PROXY_AUTH: "1",
        DD_WEB_TRUST_PROXY_ACK: "1",
      }),
    ).toBeNull();
  });

  it("自定义身份头时，拒绝信息里点名那个头", () => {
    const msg = trustProxyAuthGuard({
      ...BASE,
      DD_WEB_TRUST_PROXY_AUTH: "1",
      DD_WEB_USER_HEADER: "X-Forwarded-User",
    });
    expect(msg).toContain("x-forwarded-user");
  });
});
