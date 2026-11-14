#!/usr/bin/env node
// An end-to-end check of live control through the page itself, in headless Chromium: start a
// paused session from the "New live session" form, add a household, run a week, apply a price
// shock, capture the page, and stop the session. Needs a dashboard serving with live sessions
// (make nix-venv-dashboard). Prints what it saw and any console error; exits 1 on a failure.
//
//   node dashboard/bin/live-check.mjs http://127.0.0.1:8890/ out.png [days]

import { chromium } from "playwright-core";

const [base, out, days = "30"] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
page.on("pageerror", (e) => errors.push(String(e)));
const seen = {};
const state = () => page.locator(".live-state").textContent();
const result = () => page.locator(".live-result").textContent();
const until = async (what, fn, ms = 240000) => {
  const end = Date.now() + ms;
  for (;;) {
    const v = await fn().catch(() => null);
    if (v) return v;
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await page.waitForTimeout(500);
  }
};
try {
  await page.goto(base);
  await page.locator(".new-session > summary").click();
  await page.locator(".new-session input[type=number]").first().fill(days);
  await page.locator(".new-session button[type=submit]").click();
  seen.started = await until("the paused session", async () => /Day 0 .* paused/.test(await state()) && await state());
  const add = page.locator(".live-actions details").nth(3);
  await add.locator("summary").click();
  await add.locator("select").first().selectOption("saver");
  await add.locator("input").first().fill("400");
  await add.locator("button[type=submit]").click();
  seen.added = await until("the household", async () => /add_household applied|intervene applied/.test(await result()) && await result());
  await page.locator(".live-row button", { hasText: "+7 days" }).click();
  seen.stepped = await until("a week", async () => /Day 7 .* paused/.test(await state()) && await state());
  const shock = page.locator(".live-actions details").nth(0);
  await shock.locator("summary").click();
  await shock.locator("input").first().fill("0.7");
  await shock.locator("button[type=submit]").click();
  seen.shocked = await until("the shock", async () => /intervene (applied|refused)/.test(await result()) && await result());
  await page.locator(".live-row button", { hasText: "+1 day" }).click();
  await until("the shocked day", async () => /Day 8 .* paused/.test(await state()));
  await page.waitForTimeout(2500);
  seen.markers = await page.locator(".card").first().locator(".marker").count();
  seen.tiles = await page.locator(".tile-value").first().textContent();
  await page.screenshot({ path: out, fullPage: true });
  const stop = page.locator(".live-row button", { hasText: "Stop" });
  await stop.click();
  await stop.click();
  seen.stopped = await until("the stop", async () => /Ended/.test(await state()) && await state());
  seen.status = await page.locator("#status").textContent();
} catch (e) {
  errors.push(String(e));
  await page.screenshot({ path: out, fullPage: true }).catch(() => {});
}
console.log(JSON.stringify({ seen, errors }, null, 1));
await browser.close();
process.exit(errors.length ? 1 : 0);
