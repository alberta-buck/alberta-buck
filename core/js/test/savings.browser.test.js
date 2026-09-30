// The Savings tab in a real browser against a real sim server: the page
// served BY the server (--static) opens on the Savings tab without booting
// the in-tab world, watches a fresh world build and run, saves $10,000 in
// the basket with a key of its own (faucet, approve, deposit: one batch,
// landing between simulated days), pauses and steps the world, switches the
// wheel to L1 gas, arms a shock, redeems -- then reloads and finds the same
// world, its charts replayed and its receipt kept.  No console errors, no
// sideways scroll on a phone.
//
//   make nix-sandbox-savings-test     # builds the page, starts a server, runs this
//
// Skips unless SAVINGS_PAGE names the server's page (and outside the
// flake's shell: no browser).

import { test } from "node:test";
import assert from "node:assert/strict";

let chromium = null;
try {
  ({ chromium } = await import("playwright-core"));
} catch {
  // not installed
}
const PAGE = process.env.SAVINGS_PAGE;
const skip = !PAGE ? "no sim server (SAVINGS_PAGE; make nix-sandbox-savings-test)"
  : !process.env.PLAYWRIGHT_BROWSERS_PATH || !chromium ? "no browser (run inside the flake's shell)" : false;

const WORLD_MS = 600_000;           // a world builds in about a minute; a loaded machine, more
const DAY_MS = 240_000;             // a simulated day takes 10-60 s

test("the Savings tab: watch, save, pause and step, shock, redeem, reload", { skip, timeout: 3_600_000 },
  async (t) => {
    const browser = await chromium.launch();
    t.after(() => browser.close());
    const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    const world = `test-${Date.now().toString(16)}`;
    const url = `${PAGE.replace(/\/?$/, "/")}?world=${world}`;
    await page.goto(url);

    const idle = () => page.waitForFunction(
      () => !document.getElementById("status").classList.contains("busy"), null, { timeout: DAY_MS });
    const status = () => page.textContent("#status");
    const day = () => page.evaluate(() => globalThis.savings?.rows.at(-1)?.day ?? -1);
    const nextDay = async () => {
      const d = await day();
      await page.waitForFunction((d0) => (globalThis.savings?.rows.at(-1)?.day ?? -1) > d0, d, { timeout: DAY_MS });
    };

    await page.waitForSelector("[role=tab][aria-selected=true]");   // boot asked where its world is
    assert.equal(await page.getAttribute("#tab-savings", "aria-selected"), "true",
      "a page served by the sim server opens on the Savings tab");
    assert.ok(await page.isHidden("#loading"), "the Savings tab needs none of the in-tab world");
    assert.equal(await page.evaluate(() => globalThis.sandbox), undefined, "the in-tab world did not boot");
    for (const t of ["issuer", "wallets", "credit", "observer"]) {
      assert.equal(await page.locator(`#tab-${t}`).count(), 0, `the savings sandbox has no ${t} tab`);
    }
    assert.match(await page.textContent("#panel-savings"), /sandbox\.albertabuck\.ca/,
      "it points at the in-browser sandbox for the other tools");

    await page.waitForSelector("#panel-savings .kpis .stat", { timeout: WORLD_MS });
    await page.waitForFunction(() => globalThis.savings?.holdings, null, { timeout: WORLD_MS });
    assert.match(await page.textContent("#panel-savings"), /Your key \(simulated, kept in this browser\): 0x/);
    assert.match(await page.textContent("#panel-savings"), /shares of the basket's equity/,
      "the savings world runs the equity basket");
    await nextDay();
    assert.ok(await page.locator("#panel-savings .chart path.line").count() > 10, "the charts draw");
    const panelText = await page.textContent("#panel-savings");
    for (const title of ["The basket's credit (BUCK)", "What the basket's wheel did", "The director's lean",
                         "The desk's signal", "The desk's operations", "The desk's return", "The undertakings' return"]) {
      assert.ok(panelText.includes(title), `the ${title} chart is on the page`);
    }
    assert.equal(await page.locator("#panel-savings .chart-grid.small").count(), 2,
      "the director's lean and the commodities, one small chart per commodity");
    // Hover: a guide at the nearest day, and the legend reads that day.
    const plot = page.locator("#panel-savings .chart .plot").first();
    const box = await plot.boundingBox();
    await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
    assert.match(await page.locator("#panel-savings .chart .legend").first().textContent(), /day \d+/,
      "hovering reads the hovered day");
    await page.mouse.move(0, 0);

    // Save: the basket's choice, retried a day at a time while every pool is
    // past the deposit guard (the world's first days move the pools a lot).
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
    assert.match(await status(), /^Saved: receipt #\d+, \$10,000\.00 of \w+: the basket's equity, drawing [\d.,kM]+ BUCK of credit/);
    const receiptRow = page.locator("#panel-savings li.receipt").first();
    await receiptRow.locator("button", { hasText: "Redeem" }).waitFor({ timeout: DAY_MS });
    await page.waitForFunction(() => /\$[\d,]+\.\d\d/.test(
      document.querySelector("#panel-savings li.receipt dd.worth")?.textContent ?? ""), null, { timeout: DAY_MS });

    // Pause, then one day, then paused again.
    await page.click("#panel-savings button:text-is('Pause')");
    await idle();
    await page.waitForFunction(() => /paused at day/.test(document.querySelector("#panel-savings .card-head .chip").textContent),
      null, { timeout: DAY_MS });
    const d0 = await day();
    await page.click("#panel-savings button:text-is('1 day')");
    await idle();
    await page.waitForFunction((d) => (globalThis.savings?.rows.at(-1)?.day ?? -1) === d + 1, d0, { timeout: DAY_MS });
    await page.waitForFunction(() => /paused at day/.test(document.querySelector("#panel-savings .card-head .chip").textContent),
      null, { timeout: DAY_MS });
    assert.equal(await day(), d0 + 1, "a step runs exactly one day");

    // The wheel's gas, live; a shock, armed.
    await page.click("#panel-savings button[data-chain='l1']");
    await idle();
    await page.waitForFunction(() => /L1 gas/.test(document.querySelector("#panel-savings .card-head .chip").textContent),
      null, { timeout: 30_000 });
    assert.equal(await page.getAttribute("#panel-savings button[data-chain='l1']", "aria-pressed"), "true");
    await page.click("#panel-savings button:text-is('BUCK demand')");
    await idle();
    assert.match(await status(), /Queued/);

    // Redeem (the world is paused: it lands at once).
    await receiptRow.locator("button", { hasText: "Redeem" }).click();
    await idle();
    // In BUCK (to the key, enrolled as an identity at its first exit); in
    // kind, TOKEN, only when the basket's account cannot spend the BUCK.
    assert.match(await status(), /^Redeemed #\d+: paid \$[\d,]+\.\d\d in BUCK/);
    assert.match(await receiptRow.textContent(), /paid on day \d+/);
    assert.match(await page.textContent("#panel-savings dl.wallet"), /\(\$[\d,]+\.\d\d\)/, "the wallet holds the payout");

    // Reload: the same world, its days replayed, the receipt remembered.
    const days = await page.evaluate(() => globalThis.savings.rows.length);
    await page.reload();
    await page.waitForFunction((n) => (globalThis.savings?.rows.length ?? 0) >= n, days, { timeout: 60_000 });
    assert.match(await page.textContent("#panel-savings ul.receipts"), /paid on day \d+/);

    // A phone: no sideways scroll.
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(300);
    const over = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    assert.ok(over <= 1, `the page scrolls sideways on a phone by ${over}px`);

    await page.click("#panel-savings button:text-is('Run')");
    await idle();
    assert.deepEqual(errors, []);
  });
