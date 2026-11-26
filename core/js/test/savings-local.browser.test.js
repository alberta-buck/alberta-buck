// The Savings tab with its world in the tab (doc/review/bucknet-economy-implementation.org, "The
// savings world in the tab"): the STATIC page (no sim server: sim-server.json says null) opens the
// Savings tab, builds the savings world in a Web Worker on webevm, watches it run, saves $10,000 in
// the basket with a key of its own (faucet, approve, deposit: one batch, between days), pauses and
// steps the world, executes demand and supply shocks and redeems in BUCK. No console
// errors. The page is served from build output on an ephemeral port.
//
//   make nix-sandbox-build && cd core/js && node --test test/savings-local.browser.test.js

import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { existsSync, readFileSync } from "node:fs";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

let chromium = null;
try { ({ chromium } = await import("playwright-core")); } catch { /* not installed */ }
const DIST = fileURLToPath(new URL("../sandbox/dist/", import.meta.url));
const skip = !existsSync(join(DIST, "savings-world.js")) ? "build the sandbox first (make nix-sandbox-build)"
  : !process.env.PLAYWRIGHT_BROWSERS_PATH || !chromium ? "no browser (run inside the flake's shell)" : false;

const WORLD_MS = 180_000;
const DAY_MS = 60_000;
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json",
                ".wasm": "application/wasm", ".txt": "text/plain" };

function serve() {
  const server = createServer((req, res) => {
    const path = normalize(decodeURIComponent(new URL(req.url, "http://x").pathname)).replace(/^\/+/, "");
    const file = join(DIST, path || "index.html");
    if (!file.startsWith(DIST) || !existsSync(file)) { res.writeHead(404).end(); return; }
    res.writeHead(200, { "content-type": TYPES[extname(file)] ?? "application/octet-stream" });
    res.end(readFileSync(file));
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

test("the Savings tab runs its world in the tab: watch, save, pause and step, shock, redeem",
  { skip, timeout: 900_000 }, async (t) => {
    const server = await serve();
    t.after(() => server.close());
    const browser = await chromium.launch();
    t.after(() => browser.close());
    const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    await page.goto(`http://127.0.0.1:${server.address().port}/?tab=savings`);

    const idle = () => page.waitForFunction(
      () => !document.getElementById("status").classList.contains("busy"), null, { timeout: DAY_MS });
    const status = () => page.textContent("#status");
    const day = () => page.evaluate(() => globalThis.savings?.rows.at(-1)?.day ?? -1);
    const nextDay = async () => {
      const d = await day();
      await page.waitForFunction((d0) => (globalThis.savings?.rows.at(-1)?.day ?? -1) > d0, d, { timeout: DAY_MS });
    };

    await page.waitForSelector("#tab-savings[aria-selected=true]", { timeout: 60_000 });
    assert.equal(await page.locator("#tab-wallets").count(), 1, "the static sandbox keeps its other tools");
    await page.waitForSelector("#panel-savings .kpis .stat", { timeout: WORLD_MS });
    await page.waitForFunction(() => globalThis.savings?.info?.local && globalThis.savings?.holdings, null,
      { timeout: WORLD_MS });
    assert.match(await page.textContent("#panel-savings"), /shares of the basket's equity/);
    await nextDay();
    await nextDay();
    assert.ok(await page.locator("#panel-savings .chart path.line").count() > 10, "the charts draw");
    const households = await page.evaluate(() => globalThis.savings.rows.at(-1).households);
    assert.ok(households.active >= 2, `households ${JSON.stringify(households)}`);

    // Save: retried a day at a time while every pool is past the deposit guard.
    await page.fill("#panel-savings input[aria-label='dollars to save']", "10000");
    let saved = false;
    for (let tries = 0; tries < 10 && !saved; tries++) {
      await page.click("#panel-savings button:text-is('Save')");
      await idle();
      saved = (await page.getAttribute("#status", "class")).includes("ok");
      if (!saved) {
        assert.match(await status(), /average/, `only the guard refuses a deposit: ${await status()}`);
        await nextDay();
      }
    }
    assert.ok(saved, "a deposit landed");
    assert.match(await status(), /^Saved: receipt #\d+, \$10,000\.00 of \w+/);
    const receiptRow = page.locator("#panel-savings li.receipt").first();
    await receiptRow.locator("button", { hasText: "Redeem" }).waitFor({ timeout: DAY_MS });

    // Pause, then one day, then paused again.
    await page.click("#panel-savings button:text-is('Pause')");
    await idle();
    await page.waitForFunction(() => /paused at day/.test(document.querySelector("#panel-savings .card-head .chip").textContent),
      null, { timeout: DAY_MS });
    const d0 = await day();
    await page.click("#panel-savings button:text-is('1 day')");
    await idle();
    await page.waitForFunction((d) => (globalThis.savings?.rows.at(-1)?.day ?? -1) === d + 1, d0, { timeout: DAY_MS });
    assert.equal(await day(), d0 + 1, "a step runs exactly one day");

    // Redeem while paused, in BUCK to the enrolled key.
    await receiptRow.locator("button", { hasText: "Redeem" }).click();
    await idle();
    assert.match(await status(), /^Redeemed #\d+: paid \$[\d,]+\.\d\d in BUCK/);
    assert.match(await page.textContent("#panel-savings dl.wallet"), /\(\$[\d,]+\.\d\d\)/, "the wallet holds the payout");

    // Each shock must trade in the next close after submission.
    await page.getByLabel("shock size, $ millions").fill("0.1");
    await page.getByLabel("shock days").fill("1");
    for (const [button, field] of [["BUCK supply", "shockSold"], ["BUCK demand", "shockBought"]]) {
      const before = await page.evaluate((key) => globalThis.savings.rows.at(-1)[key], field);
      const scheduledDay = (await day()) + 1;
      await page.getByRole("button", { name: button, exact: true }).click();
      await idle();
      assert.match(await status(), /Queued/);
      await page.getByRole("button", { name: "1 day", exact: true }).click();
      await idle();
      await page.waitForFunction((d) => globalThis.savings.rows.at(-1).day === d,
        scheduledDay, { timeout: DAY_MS });
      const after = await page.evaluate((key) => globalThis.savings.rows.at(-1)[key], field);
      if (field === "shockBought") {
        assert.equal(after - before, 100_000, `${button} spent the requested USDC at close ${scheduledDay}`);
      } else {
        assert.ok(after > before, `${button} received USDC by close ${scheduledDay}`);
      }
    }

    await page.click("#panel-savings button:text-is('Run')");
    await idle();
    await nextDay();

    // The pace, live: 0 s a day runs the world as fast as it goes.
    await page.fill("#panel-savings input[aria-label='seconds per day']", "0");
    await page.dispatchEvent("#panel-savings input[aria-label='seconds per day']", "change");
    await idle();
    await page.waitForFunction(() => globalThis.savings?.status?.pace === 0, null, { timeout: DAY_MS });

    // A new world with a farm borrower and simulated commodity cycles.
    assert.equal(await page.evaluate(() => globalThis.savings.info.ticks_per_day), 4, "four ticks a day by default");
    const oldReference = await page.evaluate(() => globalThis.savings.rows[0].ref);
    await page.getByText("World settings", { exact: true }).click();
    await page.getByLabel("ticks per day", { exact: true }).selectOption("2");
    await page.getByLabel("borrower scenario").selectOption("farm");
    await page.getByLabel("prices", { exact: true }).selectOption("three-phase");
    page.once("dialog", (dlg) => dlg.accept());
    await page.getByRole("button", { name: "New world", exact: true }).click();
    await page.waitForFunction(() => globalThis.savings?.info?.borrower === "farm"
      && globalThis.savings.rows[1]?.borrower, null, { timeout: WORLD_MS });
    const { info, rows } = await page.evaluate(() => ({ info: globalThis.savings.info, rows: globalThis.savings.rows.slice(0, 2) }));
    assert.equal(info.ticks_per_day, 2);
    assert.equal(info.prices[0], "three-phase");
    assert.notDeepEqual(rows[0].ref, oldReference, "the worker applies the selected tape");
    assert.notDeepEqual(rows[0].ref, rows[1].ref, "the commodity reference prices move");
    assert.equal(rows[0].borrower.baseline, 3_000_000);
    assert.ok(rows[1].borrower.paid > rows[0].borrower.paid, "the farmer pays the mortgage lender on the first payday");
    assert.ok(rows[1].borrower.mortgage < rows[0].borrower.mortgage, "the confirmed mortgage principal falls");
    await page.getByRole("heading", { name: "Farm land mortgage" }).waitFor();
    await page.getByText("BUCK principal outstanding", { exact: true }).waitFor();
    assert.ok(await page.locator("figure", { hasText: "Debt remaining (USD)" }).locator("path.line").count() >= 2);
    assert.deepEqual(errors, []);
  });
