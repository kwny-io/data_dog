import { expect, test } from "@playwright/test";

/**
 * 调查台：**refs 证据引用面板**（刚落地的那个）与**异步调查轮询**。
 *
 * 这两件事 jsdom 都测不到：面板的"点了才请求"要靠真浏览器才知道请求有没有发出去；
 * 异步轮询要真的经过"POST 202 → 轮询 job → 取 result"三个跨进程往返。
 */

const CANDIDATE_ROW = /查看候选 .* 详情/;

test.describe("调查台：refs 证据引用面板", () => {
  test("懒解析：不点不请求；点了解析出引用、展开能看正文", async ({ page }) => {
    const refsCalls: string[] = [];
    page.on("request", (req) => {
      if (/\/api\/dd\/v1\/candidates\/[^/]+\/refs$/.test(new URL(req.url()).pathname)) {
        refsCalls.push(req.url());
      }
    });

    await page.goto("/investigate");
    await page.getByRole("button", { name: CANDIDATE_ROW }).first().click();

    await expect(page.getByRole("heading", { name: "证据引用" })).toBeVisible();
    // 懒：还没解析，而且**确实一个请求都没发**——解析 rawlog 会读正文、留审计痕迹，
    // 渲染时自动打是不可接受的。
    await expect(page.getByText(/尚未解析/)).toBeVisible();
    expect(refsCalls).toHaveLength(0);

    await page.getByRole("button", { name: "解析证据引用" }).click();
    await expect(page.getByRole("button", { name: "重新解析" })).toBeVisible();
    expect(refsCalls).toHaveLength(1);

    // 两条引用（metricwin + rawlog）都解析出来。
    await expect(page.getByRole("button", { name: /metricwin/ })).toBeVisible();
    const rawlog = page.getByRole("button", { name: /rawlog/ });
    await expect(rawlog).toBeVisible();

    // 展开 rawlog：正文、字节数，且明确"未截断"（不假装完整）。
    await rawlog.click();
    const body = page.getByTestId("ref-body");
    await expect(body).toBeVisible();
    await expect(body).toContainText("[mock] evt_ab12cd34ef56_1003");
    await expect(body).toContainText(/字节 \d+/);
    await expect(page.getByTestId("ref-truncated")).toHaveCount(0);
  });

  test("没有可解析引用的候选：解析后是空状态，不假装有内容", async ({ page }) => {
    await page.goto("/investigate");
    // 第 4 条 devmock 候选的 evidence_refs 是空数组。
    await page.getByRole("button", { name: CANDIDATE_ROW }).nth(3).click();
    await page.getByRole("button", { name: "解析证据引用" }).click();
    await expect(page.getByText("该候选没有可解析的证据引用。")).toBeVisible();
  });
});

test.describe("调查台：异步调查（202 + 轮询）", () => {
  test("提交 → 轮询 → 报告，整条链经服务端代理", async ({ page }) => {
    const submits: string[] = [];
    const polls: string[] = [];
    page.on("request", (req) => {
      const path = new URL(req.url()).pathname;
      if (path === "/api/dd/v1/investigations" && req.method() === "POST")
        submits.push(path);
      if (/^\/api\/dd\/v1\/investigations\/jobs\/[^/]+$/.test(path)) polls.push(path);
    });

    await page.goto("/investigate");
    await page.getByRole("button", { name: CANDIDATE_ROW }).first().click();

    // 先等第一次轮询请求真的发出：这是"异步路径被走到"的确证，
    // 而不是同步模式下 POST 直接带回结果（那样这条测试等于没测轮询）。
    const firstPoll = page.waitForRequest(
      /\/api\/dd\/v1\/investigations\/jobs\/[^/]+$/,
    );
    await page.getByRole("button", { name: /开始调查/ }).click();
    await firstPoll;

    // 终态：轨迹、一句话结论与报告渲染出来。
    await expect(page.getByRole("heading", { name: "ReAct 轨迹" })).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.getByRole("heading", { name: "一句话结论" })).toBeVisible();
    await expect(page.getByText("⚠️ 检测到 1 条严重异常，建议人工复核")).toBeVisible();

    expect(submits).toHaveLength(1);
    // devmock 头两次轮询报 running，所以真的轮询了不止一次才拿到结果。
    expect(polls.length).toBeGreaterThanOrEqual(2);
  });
});
