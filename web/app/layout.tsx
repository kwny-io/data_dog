import type { Metadata } from "next";

import { Providers, themeInitScript } from "@/lib/providers";
import { resolveProxyConfig } from "@/lib/api/proxy";
import { techInitScript } from "@/lib/tech-fields";
import { siteOrigin } from "@/lib/site";
import { SmoothScroll } from "@/lib/smooth-scroll";
import { AppShell } from "@/components/AppShell";
import { ConsoleBackground } from "@/components/ConsoleBackground";

import "./globals.css";

// 每个请求都重新渲染：根布局要读**服务端**环境变量（DD_WEB_TENANT）来决定展示的租户。
// 若被构建期静态化，租户会被烤进 HTML —— 改了环境变量却看不到变化，是那种"不报错但
// 行为不对"的故障。控制台本来就是内部实时工具，SSR 的代价可以接受。
export const dynamic = "force-dynamic";

/* Type is the platform's own UI face (see --font-ui in globals.css). SF Pro is
 * not licensed for the web, so the stack resolves to -apple-system on Apple
 * hardware and to the native UI font elsewhere: authentic, and zero font bytes
 * to ship. */

const TITLE = "遥测故障调查台";
const DESCRIPTION = "机器狗遥测异常候选调查控制台——结论挂证据，状态可追溯。";

export const metadata: Metadata = {
  // metadataBase is what turns every relative URL below (canonical, og:url,
  // og:image) into an absolute one. Without it Next falls back to a localhost
  // origin at build time, and share cards go out pointing at the wrong host.
  metadataBase: new URL(siteOrigin()),
  title: { default: TITLE, template: `%s · ${TITLE}` },
  description: DESCRIPTION,
  applicationName: TITLE,
  // Internal operations console: it should not be indexed or crawled.
  robots: { index: false, follow: false },
  alternates: { canonical: "/" },
  openGraph: {
    type: "website",
    siteName: TITLE,
    title: TITLE,
    description: DESCRIPTION,
    url: siteOrigin(),
    locale: "zh_CN",
  },
  twitter: {
    card: "summary",
    title: TITLE,
    description: DESCRIPTION,
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // 租户在**服务端**解析（DD_WEB_TENANT）；开了 DD_WEB_TENANTS 允许清单后，
  // 浏览器可用 cookie 在清单内选择租户（由代理执行并校验）。两个值都传给客户端：
  // 默认值用于 SSR，允许清单用于算出**真实生效**的租户来展示。
  const { tenant, tenants } = resolveProxyConfig();
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        {/* set the dark class before first paint to avoid a flash */}
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
        {/* mark the raw-value flag before first paint, same reason */}
        <script dangerouslySetInnerHTML={{ __html: techInitScript }} />
      </head>
      <body className="min-h-dvh antialiased">
        <ConsoleBackground />
        <SmoothScroll />
        <Providers tenant={tenant} tenants={tenants}>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
