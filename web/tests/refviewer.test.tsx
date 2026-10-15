import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EvidenceRefsPanel } from "@/components/RefViewer";

/**
 * 证据引用面板：**懒解析** + **如实呈现**（截断与失败都要看得见）。
 *
 * 之所以把"没点按钮就不许请求"单测：解析 rawlog 会在服务端读原始日志正文并**留审计**，
 * 若一渲染就自动请求，审计轨迹会被噪音灌满、还会拉回一堆没人要的正文。
 */
const h = vi.hoisted(() => ({ calls: [] as boolean[], state: {} as Record<string, unknown> }));

vi.mock("@/lib/api/queries", () => ({
  useCandidateRefs: (_id: string, opts?: { enabled?: boolean }) => {
    h.calls.push(Boolean(opts?.enabled));
    return h.state;
  },
}));

function setState(state: Record<string, unknown>) {
  h.state = { isPending: false, isSuccess: false, isError: false, isFetching: false, data: undefined, refetch: vi.fn(), ...state };
}

beforeEach(() => {
  h.calls.length = 0;
});

describe("EvidenceRefsPanel", () => {
  it("渲染时不解析；点了才把 enabled 打开", () => {
    setState({ isPending: true });
    render(<EvidenceRefsPanel candidateId="cand-1" refs={["rawlog://evt-1"]} />);
    expect(screen.getByText(/尚未解析/)).toBeTruthy();
    expect(h.calls.at(-1)).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: /解析证据引用/ }));
    expect(h.calls.at(-1)).toBe(true);
  });

  it("展开 rawlog：正文、截断与摘要都显示（未展开时正文不可见）", () => {
    setState({
      isSuccess: true,
      data: {
        ok: true, candidate_id: "cand-1", resolved: 1, total: 1,
        results: [{
          ok: true, source: "rawlog",
          ref: { scheme: "rawlog", authority: "evt-1", path: [] },
          payload: {
            content: "motor overheat at 10:03", bytes: 22, truncated: true,
            content_sha256: "a".repeat(64),
            note: "正文超过上限 65536 字节，已截断（完整 90000 字节）",
          },
        }],
      },
    });
    render(<EvidenceRefsPanel candidateId="cand-1" refs={["rawlog://evt-1"]} />);
    fireEvent.click(screen.getByRole("button", { name: /解析证据引用/ }));
    expect(screen.queryByText(/motor overheat at 10:03/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /evt-1/ }));
    expect(screen.getByText(/motor overheat at 10:03/)).toBeTruthy();
    expect(screen.getByTestId("ref-truncated")).toBeTruthy();
    expect(screen.getByText(/sha256 aaaaaaaaaaaaaaaa/)).toBeTruthy();
    expect(screen.getByText(/已截断（完整 90000 字节）/)).toBeTruthy();
  });

  it("失败的引用显示后端错误码，不假装有内容", () => {
    setState({
      isSuccess: true,
      data: {
        ok: true, candidate_id: "cand-1", resolved: 0, total: 1,
        results: [{
          ok: false,
          ref: { scheme: "metricwin", authority: "cand-1", path: [] },
          error: "not_found", detail: "窗口内无数据",
        }],
      },
    });
    render(<EvidenceRefsPanel candidateId="cand-1" refs={["metricwin://cand-1"]} />);
    fireEvent.click(screen.getByRole("button", { name: /解析证据引用/ }));
    expect(screen.getByText("not_found")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /cand-1/ }));
    expect(screen.getByText(/解析失败：not_found — 窗口内无数据/)).toBeTruthy();
  });
});
