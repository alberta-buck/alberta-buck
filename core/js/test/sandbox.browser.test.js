// The sandbox page in a real browser (headless Chromium through Playwright,
// both from the flake): the built page boots a world, and a person can
// certify two people, give each a wallet, introduce them, be refused a
// payment they cannot make, open trading, buy BUCK and pay; insure a home,
// be quoted its premium and the shortfall, buy it and activate credit; watch
// thirty days depreciate it; find no name in the observer's view of it all
// -- then reload and find the same world.  No console errors, no sideways
// scroll on a phone.
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

    // Credit: Sandbox Mutual insures Carol's home at the class defaults.
    await page.click("#tab-credit");
    await page.selectOption("#panel-credit select[data-key='credit:holder']", "W1");
    await ok(page.locator("#panel-credit form button", { hasText: "Insure" }), /^Insured: credit #1\.$/);
    await page.selectOption("#panel-credit select[data-key='credit:who']", "W1");
    await page.fill("#panel-credit input[data-key='credit:amount']", "50000");
    const buyFirst = page.locator("#panel-credit .quote button", { hasText: "first" });
    await buyFirst.waitFor({ timeout: 30_000 });
    assert.match(await page.textContent("#panel-credit .quote"), /Premium\s*1,81\d\.\d\d/);
    await ok(buyFirst, /^Bought [\d,.]+ BUCK for [\d,.]+ USDC\.$/);
    const activate = page.locator("#panel-credit .quote button", { hasText: "Activate" });
    await activate.waitFor({ timeout: 30_000 });
    await ok(activate, /premium 1,81\d\.\d\d BUCK paid into the insurance pool/);
    const worth = async () => (await page.locator("#panel-credit tbody td").nth(7).textContent()).trim();
    const was = await worth();
    await ok(page.locator("button", { hasText: "+30 days" }), /Thirty days passed/);
    assert.notEqual(await worth(), was, "the home depreciated");

    // Observer: everything public, identity opaque, and no name -- unless
    // you ask to see what only you know.
    await page.click("#tab-observer");
    const seen = await page.textContent("#panel-observer .txs");
    for (const s of ["Carol", "Nakamura", "Bob", "Tremblay", "1994-10-27", "AB-P-"]) {
      assert.ok(!seen.includes(s), `the observer shows ${s}`);
    }
    assert.ok(await page.locator("#panel-observer .opaque").count() > 10);
    await page.check("#observer-knows");
    const known = await page.textContent("#panel-observer .txs");
    for (const label of ["Carol's wallet", "BUCK/USDC pool", "Permit2", "Insurance pool"]) {
      assert.ok(known.includes(label), `no label ${label}`);
    }
    await page.uncheck("#observer-knows");
    await page.selectOption("#panel-observer select", "market");
    assert.match(await page.textContent("#panel-observer .txs"), /amount\s*unlimited/,
      "the Permit2 approvals, filed under the market");
    await page.selectOption("#panel-observer select", "all");

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
