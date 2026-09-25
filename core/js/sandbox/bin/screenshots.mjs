#!/usr/bin/env node
// The sandbox's screenshots for doc/SANDBOX.org: serve the built page, play
// a short story in headless Chromium (the flake's), and capture each tool.
//
//   make nix-sandbox-screenshots       # writes images/sandbox/*.png
//
// The story: certify Carol and Bob, a wallet each, introduce them; Carol
// opens trading, buys BUCK and pays Bob; Sandbox Mutual insures her home,
// she buys the premium's shortfall and activates 50,000 BUCK of credit;
// thirty days pass.  Person numbers and keys are fresh on every run.

import { createServer } from "node:http";
import { mkdirSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

const DIST = fileURLToPath(new URL("../dist", import.meta.url));
const OUT = resolve(process.argv[2] ?? "images/sandbox");
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
                ".wasm": "application/wasm" };

const server = createServer(async (req, res) => {
  const path = new URL(req.url, "http://x").pathname;
  const p = join(DIST, decodeURIComponent(path.endsWith("/") ? `${path}index.html` : path));
  try {
    const body = await readFile(p);
    res.writeHead(200, { "content-type": TYPES[extname(p)] ?? "application/octet-stream" });
    res.end(body);
  } catch {
    res.writeHead(404).end();
  }
}).listen(0, "127.0.0.1");
await new Promise((r) => server.once("listening", r));

mkdirSync(OUT, { recursive: true });
const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1200, height: 800 } })).newPage();
page.on("pageerror", (e) => console.error("page error:", e.message));
await page.goto(`http://127.0.0.1:${server.address().port}/`);
await page.waitForSelector("#loading", { state: "hidden", timeout: 240_000 });
// Captured whole, the sticky world bar would land mid-page: pin it to the top.
await page.addStyleTag({ content: ".bar { position: static; }" });

async function act(locator) {
  await locator.click();
  await page.waitForFunction(() => !document.getElementById("status").classList.contains("busy"),
    null, { timeout: 120_000 });
  if ((await page.getAttribute("#status", "class")).includes("error")) {
    throw new Error(`refused: ${await page.textContent("#status")}`);
  }
}
const shot = async (name) => {
  await page.screenshot({ path: join(OUT, `${name}.png`), fullPage: true });
  console.log(`wrote ${join(OUT, `${name}.png`)}`);
};

await page.click("#tab-issuer");
for (let i = 0; i < 2; i++) {
  await page.click("text=Fill a sample person");
  await act(page.locator("button", { hasText: "Issue credential" }));
}
await act(page.locator("button", { hasText: "New wallet for Carol" }));
await shot("issuer");

await act(page.locator("button", { hasText: "New wallet for Bob" }));
await page.click("#tab-wallets");
const carol = page.locator("[data-wallet=W1]");
await act(carol.locator("button", { hasText: "Introduce" }));
await act(carol.locator("button", { hasText: "Open trading" }));
await carol.locator("input[aria-label='USDC to spend']").fill("500");
await act(carol.locator("button", { hasText: "Buy BUCK" }));
await carol.locator("input[aria-label='amount to pay']").fill("120");
await act(carol.locator("button", { hasText: "Send" }));
await shot("wallets");

await page.click("#tab-credit");
await page.selectOption("#panel-credit select[data-key='credit:holder']", "W1");
await act(page.locator("#panel-credit form button", { hasText: "Insure" }));
await page.selectOption("#panel-credit select[data-key='credit:who']", "W1");
await page.fill("#panel-credit input[data-key='credit:amount']", "50000");
const buyFirst = page.locator("#panel-credit .quote button", { hasText: "first" });
await buyFirst.waitFor({ timeout: 30_000 });
await shot("credit-quote");
await act(buyFirst);
const activate = page.locator("#panel-credit .quote button", { hasText: "Activate" });
await activate.waitFor({ timeout: 30_000 });
await act(activate);
await act(page.locator("button", { hasText: "+30 days" }));
await shot("credit");

await page.click("#tab-observer");
await page.selectOption("#panel-observer select", "identity");
await page.setViewportSize({ width: 1200, height: 1100 });
await page.screenshot({ path: join(OUT, "observer.png") });
console.log(`wrote ${join(OUT, "observer.png")}`);

await browser.close();
server.close();
