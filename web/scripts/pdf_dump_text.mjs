/**
 * 截图的内容核对器。
 *
 * 为什么需要：截图是像素，我看不到像素内容；评审也看不出"这屏是不是报错页"。
 * 所以对每一页同时抓一份**真实文本**——它与截图同一次渲染，用来证明截的不是空白页/错误页。
 *
 * 用法：node scripts/pdf_dump_text.mjs <baseUrl>
 */
import { chromium } from "playwright";

const base = process.argv[2] ?? "http://127.0.0.1:3101";
const pages = ["/", "/alerts", "/investigate", "/operators"];

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, locale: "zh-CN" });
const page = await ctx.newPage();

const out = [];
for (const path of pages) {
  const resp = await page.goto(base + path, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForTimeout(1500);
  const text = (await page.innerText("body").catch(() => "")).replace(/\n{2,}/g, "\n").trim();
  out.push({
    path,
    status: resp?.status() ?? 0,
    title: await page.title(),
    chars: text.length,
    // 报错页的特征词：截图上必须没有这些
    looksLikeError: /Application error|Unhandled Runtime Error|404|This page could not be found/.test(text),
    text: text.slice(0, 700),
  });
}

await browser.close();
console.log(JSON.stringify(out, null, 2));
