"use client";

import { useTenant } from "@/lib/providers";

/**
 * 展示**代理真实生效**的租户。
 *
 * 为什么不是只读一句话了事：`lib/api/proxy.ts::resolveTenant` 在配置了
 * `DD_WEB_TENANTS` 允许清单后，会按 `dd_web_tenant` cookie 在**清单内**选择租户
 * （清单外一律 403，失败关闭）。所以"浏览器不能选租户"只在**没配清单**时成立。
 * 早先这里无论配没配都写"浏览器无法切换"，结果是：数据走的是 tenant-alt、
 * 界面显示 tenant-demo——由真后端 e2e 腿抓到（`e2e/real-backend.spec.ts`）。
 *
 * 现在两种模式都如实说：单租户说"服务端决定"，白名单模式说"可在清单内选择"，
 * 并把允许清单列在 title 里（它不是秘密，是配置）。
 */
export function TenantBadge() {
  const { tenant, serverTenant, selectable, allowed } = useTenant();

  const title = selectable
    ? `租户由代理按 cookie（dd_web_tenant）在允许清单内选择：${allowed.join("、")}；清单外一律 403，不会静默回落到默认租户。服务端默认租户是 ${serverTenant}。`
    : "租户由服务端决定（DD_WEB_TENANT）；未配置 DD_WEB_TENANTS 时浏览器选择会被直接忽略";

  return (
    <span
      data-testid="tenant-badge"
      title={title}
      className="inline-flex h-8 items-center gap-1.5 rounded-full border border-rule bg-paper-sunken px-2.5 text-xs text-ink-muted"
    >
      <span className="text-ink-faint">租户</span>
      <span className="reading font-medium text-ink">{tenant}</span>
      {selectable && tenant !== serverTenant ? (
        <span className="text-ink-faint" title={`服务端默认：${serverTenant}`}>
          （已切换）
        </span>
      ) : null}
    </span>
  );
}
