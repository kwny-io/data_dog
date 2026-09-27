import Link from "next/link";

import { Panel } from "@/components/ui/primitives";

export default function NotFound() {
  return (
    <Panel title="未找到" className="mx-auto mt-8 max-w-xl">
      <div className="space-y-3 px-5 py-6">
        <p className="text-sm text-ink">这个地址没有对应的页面。</p>
        <p className="text-xs text-ink-muted">
          注意：后端对「不存在」和「跨租户」都返回 404，不会透露资源是否存在。
        </p>
        <Link href="/" className="inline-flex border border-rule-strong px-3 py-1.5 text-sm hover:bg-paper-sunken">
          返回仪表盘
        </Link>
      </div>
    </Panel>
  );
}
