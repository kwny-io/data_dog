"use client";

import { useTenant } from "@/lib/providers";

/**
 * 展示当前租户。
 *
 * **只读**：租户由服务端决定（`DD_WEB_TENANT`，经 `app/api/dd` 代加强制注入），
 * 浏览器改不了——代理会丢弃浏览器传来的 `X-Tenant-Id`。这里以前是一个可编辑的
 * 切换控件（写 localStorage 并影响请求头）；服务端代理落地后它已经**不会生效**，
 * 留着只会让人以为"浏览器能选租户"。多租户登录属后端开放问题（spec §9.3）。
 */
export function TenantBadge() {
  const { tenant } = useTenant();

  return (
    <span
      title="租户由服务端决定（DD_WEB_TENANT）；浏览器无法切换"
      className="inline-flex h-8 items-center gap-1.5 rounded-full border border-rule-strong bg-panel/50 px-2.5 text-xs text-ink-muted"
    >
      <span className="text-ink-faint">租户</span>
      <span className="reading font-medium text-ink">{tenant}</span>
    </span>
  );
}
