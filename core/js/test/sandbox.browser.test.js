// The sandbox page in a real browser (headless Chromium through Playwright,
// both from the flake): the built page boots a world, and a person can
// certify two people, give each a wallet, introduce them, be refused a
// payment they cannot make, open trading, buy BUCK and pay -- then reload
// and find the same world.  No console errors, no sideways scroll on a phone.
//
// Build first:  make nix-sandbox-build.  Skips outside the flake's shell
// (PLAYWRIGHT_BROWSERS_PATH) or without a built page.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { extname, join } from "node:path";
import { fileURLToPath } from "node:url";

const DIST = fileURLToPath(new URL("../sandbox/dist", import.meta.url));
let chromium = null;
try {
  ({ chromium } = await import("playwright-core"));
} catch {
  // not installed
}
const skip = !existsSync(join(DIST, "app.js")) ? "sandbox not built (make nix-sandbox-build)"
  : !process.env.PLAYWRIGHT_BROWSERS_PATH || !chromium ? "no browser (run inside the flake's shell)"
    : false;

const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
                ".wasm": "application/wasm", ".json": "application/json" };

function serve() {
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
  return new Promise((r) => server.once("listening", () => r(server)));
}

test("the sandbox page: certify, register, introduce, trade, pay, reload", { skip, timeout: 600_000 },
  async (t) => {
    const server = await serve();
    const browser = await chromium.launch();
    t.after(async () => {
      await browser.close();
      server.close();
    });
    const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
    const problems = [];
    page.on("console", (m) => { if (m.type() === "error") problems.push(m.text()); });
    page.on("pageerror", (e) => problems.push(e.message));

    const t0 = Date.now();
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForSelector("#loading", { state: "hidden", timeout: 240_000 });
    t.diagnostic(`a new world booted in ${((Date.now() - t0) / 1000).toFixed(1)} s`);

    // Click, wait for the action to settle, and return [ok?, status line].
    const act = async (locator) => {
      await locator.click();
      await page.waitForFunction(() => !document.getElementById("status").classList.contains("busy"),
        null, { timeout: 120_000 });
      const cls = await page.getAttribute("#status", "class");
      return [!cls.includes("error"), (await page.textContent("#status")).trim()];
    };
    const ok = async (locator, re) => {
      const [good, text] = await act(locator);
      assert.ok(good, `refused: ${text}`);
      if (re) assert.match(text, re);
      return text;
    };

    await page.click("#tab-issuer");
    for (let i = 0; i < 2; i++) {
      await page.click("text=Fill a sample person");
      await ok(page.locator("button", { hasText: "Issue credential" }), /^Issued C\d: AB-P-\d{4}-\d{4}\.$/);
    }
    await ok(page.locator("button", { hasText: "New wallet for Carol" }), /registered/);
    await ok(page.locator("button", { hasText: "New wallet for Bob" }), /registered/);

    await page.click("#tab-wallets");
    const carol = page.locator("[data-wallet=W1]");
    await ok(carol.locator("button", { hasText: "Introduce" }), /Introduced/);
    const pay = async () => {
      await carol.locator("input[aria-label='amount to pay']").fill("100");
      return act(carol.locator("button", { hasText: "Send" }));
    };
    const [paid, why] = await pay();
    assert.equal(paid, false, "no BUCK yet");
    assert.match(why, /exceeds spendable/);
    await ok(carol.locator("button", { hasText: "Open trading" }), /Trading open/);
    await carol.locator("input[aria-label='USDC to spend']").fill("500");
    await ok(carol.locator("button", { hasText: "Buy BUCK" }), /^Bought 49\d\.\d\d BUCK\.$/);
    const [paid2] = await pay();
    assert.equal(paid2, true);
    const bob = page.locator("[data-wallet=W2]");
    assert.match(await bob.textContent(), /BUCK held\s*100\.00\s*BUCK/);

    // The world is saved: a reload brings it back.
    const before = (await page.textContent("#stats")).replace(/\s+/g, " ");
    await page.reload();
    await page.waitForSelector("#loading", { state: "hidden", timeout: 240_000 });
    assert.equal((await page.textContent("#stats")).replace(/\s+/g, " "), before);
    await page.click("#tab-wallets");
    assert.deepEqual(await page.locator("[data-wallet] > .card-head h3").allTextContents(),
      ["Carol's wallet", "Bob's wallet"]);

    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth),
      false, "no sideways scroll on a phone");
    assert.deepEqual(problems, []);
  });
