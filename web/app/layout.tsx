import type { Metadata } from "next";

import { Providers, themeInitScript } from "@/lib/providers";
import { techInitScript } from "@/lib/tech-fields";
import { AppShell } from "@/components/AppShell";

import "./globals.css";

/* Fonts are declared with @font-face in globals.css and served from
 * public/fonts — see the comment there for why we self-host. */

export const metadata: Metadata = {
  title: { default: "data_dog · 遥测故障调查台", template: "%s · data_dog" },
  description: "机器狗遥测异常候选调查控制台——结论挂证据，状态可追溯。",
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
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
