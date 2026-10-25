import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CandidateList } from "@/components/CandidateList";
import { Providers } from "@/lib/providers";

/**
 * 接线级测试：上面 `hints.test.ts` 只证明文案函数会带出租户，
 * 这里证明**组件真的把 provider 里的租户传进去了**（provider → 组件 → 空态）。
 * 少了这一层，文案对了但页面上可能根本没有它（默认值、漏传 props、用了别的 ctx）。
 */
vi.mock("@/lib/api/queries", () => ({
  useCandidates: () => ({
    isPending: false,
    isError: false,
    data: { items: [], total: 0 },
    refetch: () => {},
  }),
}));

describe("空列表界面：租户名真的渲染出来", () => {
  it("候选空态在页面上显示当前租户，并说明空不等于故障", () => {
    render(
      <Providers tenant="tenant-xyz">
        <CandidateList
          page={{ limit: 50, offset: 0 }}
          onPage={() => {}}
          selectedId={null}
          onSelect={() => {}}
          filters={{}}
        />
      </Providers>,
    );

    // 租户名来自 Provider（服务端 DD_WEB_TENANT），不是组件内写死
    expect(screen.getByText(/tenant-xyz/)).toBeInTheDocument();
    expect(screen.getByText(/不是接口故障/)).toBeInTheDocument();
  });
});
