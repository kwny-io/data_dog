/**
 * Next.js 启动钩子（`next dev` / `next start` 都会执行 register）。
 *
 * 存在的唯一目的：把"上游反代必须剥掉客户端伪造的身份头"这条**部署前提**，从代码注释
 * 里搬进**启动期硬门**。`DD_WEB_TRUST_PROXY_AUTH=1` 而既没有门、也没有显式确认时，
 * 控制台宁可拒绝启动，也不静默信任一个无法验证的请求头（谁都能写）。
 *
 * 判定逻辑本身是纯函数（`lib/api/proxy.ts::trustProxyAuthGuard`，有单测钉判定表）；
 * 这里只负责"拿到原因就抛错"，让进程以非零码退出而不是带病启动。
 */
export async function register(): Promise<void> {
  // Edge 运行时不读这些服务端 env，也不该在这里拦。（本仓没有 Edge 路由，防御性判断。）
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  const { trustProxyAuthGuard } = await import("./lib/api/proxy");
  const reason = trustProxyAuthGuard(process.env);
  if (reason) {
    console.error(reason);
    throw new Error(reason);
  }
}
