import type { Metadata } from "next";

import { Providers, themeInitScript } from "@/lib/providers";
import { techInitScript } from "@/lib/tech-fields";
import { siteOrigin } from "@/lib/site";
import { SmoothScroll } from "@/lib/smooth-scroll";
import { AppShell } from "@/components/AppShell";
import { ConsoleBackground } from "@/components/ConsoleBackground";

import "./globals.css";

/* Fonts are declared with @font-face in globals.css and served from
 * public/fonts — see the comment there for why we self-host. */

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
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
