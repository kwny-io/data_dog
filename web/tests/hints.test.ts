import { describe, expect, it } from "vitest";

import { emptyAlertsHint, emptyCandidatesHint } from "@/lib/hints";

/**
 * 空列表有两种含义，界面必须说清是哪一种：
 *  1) 真的没数据；
 *  2) **租户不对**（多租户隔离正常工作）。
 * 本仓库真实踩过第 2 种：`make ingest` 投 `tenant-real`、控制台读 `tenant-demo`，
 * 照 README 跑完演示列表永远为空，被当成"接口坏了"排查。
 * 这里把"必须点名当前租户 + 必须说这不是故障"钉死。
 */
describe("空态文案：必须指名租户并排除误判", () => {
  it("候选空态点出当前租户", () => {
    const hint = emptyCandidatesHint("tenant-demo");
    expect(hint).toContain("tenant-demo");
  });

  it("候选空态点出另一个租户时也如实显示（不是写死的常量）", () => {
    const hint = emptyCandidatesHint("tenant-acme");
    expect(hint).toContain("tenant-acme");
    expect(hint).not.toContain("tenant-demo");
  });

  it("候选空态明确说'不是接口故障'，并给出下一步动作", () => {
    const hint = emptyCandidatesHint("tenant-demo");
    expect(hint).toContain("不是接口故障");
    expect(hint).toContain("make ingest");
  });

  it("告警空态同样点出租户并排除误判", () => {
    const hint = emptyAlertsHint("tenant-demo");
    expect(hint).toContain("tenant-demo");
    expect(hint).toContain("不是接口故障");
  });

  it("有状态筛选时，告警空态改为解释筛选而不是讲租户（避免答非所问）", () => {
    const hint = emptyAlertsHint("tenant-demo", "已确认");
    expect(hint).toContain("已确认");
    expect(hint).toContain("去掉筛选");
    expect(hint).not.toContain("tenant-demo");
  });
});
