/**
 * 空态文案（纯函数，便于单测钉住"说得清"）。
 *
 * 为什么单独抽出来：空列表有两种完全不同的含义——
 *  1) 真的没有数据（还没投递 / 筛选条件过窄）；
 *  2) **租户不对**：数据投到了别的租户，多租户隔离正常工作。
 * 两者在界面上长得一模一样。本仓库真实发生过第 2 种：`make ingest` 投 `tenant-real`、
 * 控制台读 `tenant-demo`，照 README 跑完演示后列表永远为空，被误判成"接口坏了"。
 * 所以空态必须把**当前租户**和**"这不是故障"**都说出来，且这句话要被测试钉住。
 */

/** 候选列表空态：点名当前租户 + 区分"没数据"与"租户不对"。 */
export function emptyCandidatesHint(tenant: string): string {
  return `候选由在线数据面从原始日志产出，只统计当前租户 ${tenant} 的数据。先往租户 ${tenant} 提交一批设备日志（如 make ingest）；若数据投到了别的租户，这里会一直为空——这是多租户隔离在正常工作，不是接口故障。`;
}

/**
 * 告警列表空态。
 * @param tenant 当前租户（来自服务端 DD_WEB_TENANT，浏览器改不了）
 * @param stateLabel 已选的告警状态中文名；为空表示没有筛选
 */
export function emptyAlertsHint(tenant: string, stateLabel?: string | null): string {
  if (stateLabel) {
    return `当前筛选的是「${stateLabel}」状态；去掉筛选看全部。`;
  }
  return (
    `告警由 P3 上报闭环在候选经策略门判定后产生，只统计当前租户 ${tenant}；` +
    `若数据投到了别的租户，这里会一直为空（隐私隔离在正常工作，不是接口故障）。`
  );
}

/**
 * 代理**真实生效**的租户（客户端展示用）。
 *
 * 为什么必须有这个函数：`lib/api/proxy.ts::resolveTenant` 在配置了 `DD_WEB_TENANTS`
 * 允许清单后，会按 `dd_web_tenant` cookie 在清单内选择租户；而界面（租户徽标、空态文案）
 * 原先只显示 `DD_WEB_TENANT` 服务端默认值。两者一旦不同，界面就会**指向错的租户**——
 * 空态里写着"先往租户 A 投数据"，而列表其实在查租户 B。这正是空态文案本身要解决的
 * 那个失败场景（"数据投到别的租户了"），只是换了个方向。
 *
 * 边界（与代理保持一致，宁可显示服务端租户也不显示一个会被 403 的值）：
 *  - 允许清单为空（单租户模式）：代理**直接忽略** cookie → 返回服务端租户；
 *  - cookie 不在清单内：代理会 403 失败关闭 → 界面回落到服务端租户，不显示越权值。
 */
export function effectiveTenant(
  serverTenant: string,
  allowList: readonly string[],
  cookieValue: string | null | undefined,
): string {
  if (allowList.length === 0) return serverTenant;
  const chosen = (cookieValue ?? "").trim();
  if (!chosen) return serverTenant;
  return allowList.includes(chosen) ? chosen : serverTenant;
}

/**
 * 租户选择 cookie 名。
 *
 * 与 `lib/api/proxy.ts::TENANT_COOKIE` **必须同名**（有一个单测钉住两者相等）。
 * 这里重复一份是刻意的：`proxy.ts` 是服务端模块，客户端组件（Providers/Badge）
 * 一旦 import 它，就会把服务端代码（环境变量读取、密钥拼接）拉进浏览器 bundle。
 */
export const TENANT_COOKIE_NAME = "dd_web_tenant";

/** 从 `document.cookie` 里取某个 cookie 的值（浏览器外返回 null）。 */
export function readCookieValue(cookieHeader: string | null | undefined, name: string): string | null {
  if (!cookieHeader) return null;
  for (const part of cookieHeader.split(";")) {
    const eq = part.indexOf("=");
    if (eq < 0) continue;
    if (part.slice(0, eq).trim() === name) return decodeURIComponent(part.slice(eq + 1).trim());
  }
  return null;
}
