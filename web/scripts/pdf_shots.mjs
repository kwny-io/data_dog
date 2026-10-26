/**
 * 给参赛 PDF 截真图（不是渲染图、不是设计稿）。
 *
 * 目标：拿到"产品真的在跑"的像素证据。截图对象是 `docker compose --profile web`
 * 起来的三容器链路（db + app + web），经 web 的服务端代理访问后端。
 *
 * 用法：node shots.mjs <baseUrl> <outDir>
 */
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const base = process.argv[2] ?? "http://127.0.0.1:3101";
const out = process.argv[3] ?? ".";
mkdirSync(out, { recursive: true });

const pages = [
  // 第 4 列：截之前必须出现的文本 = “数据真的到位了”的判据。截到空表格不算证据。
  ["home", "/", "首页：整条链路的入口", null],
  ["alerts", "/alerts", "告警列表（状态机筛选）", null],
  ["investigate", "/investigate", "调查台：候选 → 证据链", "device-go2-0001"],
  ["operators", "/operators", "算子控制面", null],
];

const browser = await chromium.launch();
const ctx = await browser.newContext({
  viewport: { width: 1280, height: 800 },
  deviceScaleFactor: 2, // 打印用，2x 才不糊
  locale: "zh-CN",
});
const page = await ctx.newPage();

const results = [];
// 注意：不能用 waitUntil:"networkidle"——控制台是**持续轮询**的，网络永远不会 idle，
// 用它会 30s 超时（我第一次就这么踩了）。改为等真实内容：宁可等文本出现。
for (const [name, path, note, mustSee] of pages) {
  const resp = await page.goto(base + path, { waitUntil: "domcontentloaded", timeout: 30000 });
  // 等到“加载中/刷新中”消失，最多 20s；有 mustSee 时再等该文本
  await page
    .waitForFunction(() => !/加载中|刷新中/.test(document.body.innerText), null, { timeout: 20000 })
    .catch(() => {});
  if (mustSee) {
    await page.getByText(mustSee).first().waitFor({ timeout: 20000 });
    // 调查台要点开候选才看得到证据链——这一步是真实用户操作
    await page.getByText(mustSee).first().click().catch(() => {});
    await page.waitForTimeout(1500);
  }
  await page.waitForTimeout(800);
  const stillLoading = /加载中|刷新中/.test(await page.innerText("body"));
  const file = `${out}/${name}.png`;
  await page.screenshot({ path: file, fullPage: false });
  results.push({ name, path, status: resp?.status() ?? 0, file, note, stillLoading });
}

// 就绪探针的"真 JSON"：PDF 里最有说服力的一格
const probe = await ctx.newPage();
const r = await probe.goto(base + "/api/dd/readyz", { waitUntil: "domcontentloaded", timeout: 30000 });
const body = await probe.locator("body").innerText().catch(() => "");
await probe.screenshot({ path: `${out}/readyz.png`, fullPage: true });
results.push({ name: "readyz", path: "/api/dd/readyz", status: r?.status() ?? 0, file: `${out}/readyz.png`, note: body.slice(0, 400) });

await browser.close();
console.log(JSON.stringify({ base, results }, null, 2));
