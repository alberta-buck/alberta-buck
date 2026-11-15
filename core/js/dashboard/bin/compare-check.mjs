#!/usr/bin/env node
// An end-to-end check of the Compare tab in headless Chromium, against a dashboard serving runs
// that include a comparison (python -m alberta_buck.sim.compare): open the tab, see the first
// comparison's runs drawn and tabled, vary another axis if there is one, then compare two runs
// directly and read what differs. Captures the page; prints what it saw and any console error;
// exits 1 on a failure.
//
//   node dashboard/bin/compare-check.mjs http://127.0.0.1:8891/ out.png

import { chromium } from "playwright-core";

const [base, out] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 1400 } });
const errors = [];
// A read-only dashboard (--no-live) refuses the live-session requests the page makes: not an error here.
const refused = [];
page.on("response", (r) => { if (r.status() >= 400) refused.push(`${r.status()} ${new URL(r.url()).pathname}`); });
page.on("console", (m) => { if (m.type() === "error" && !/status of 403/.test(m.text())) errors.push(m.text()); });
page.on("pageerror", (e) => errors.push(String(e)));
let failed = false;
try {
  await page.goto(new URL("?tab=compare", base).href);
  const panel = page.locator("#panel-compare");
  await panel.locator("table tbody tr").first().waitFor({ timeout: 30000 });
  const rows = await panel.locator("table tbody tr").count();
  const charts = await panel.locator(".card").count();
  const first = await panel.locator("table tbody tr th").allTextContents();
  const differs = await panel.locator(".compare-list li").allTextContents();
  console.log(`comparison: ${rows} runs (${first.join(", ")}), ${charts} charts`);
  for (const d of differs) console.log(`  ${d}`);
  if (rows < 2 || charts < 5) throw new Error("expected at least two runs and the charts");
  if (out) await page.screenshot({ path: out, fullPage: true });
  // Any two runs: the settings that differ, from their manifests.
  await panel.locator("select").first().selectOption("pair");
  await panel.locator(".compare-list li").first().waitFor({ timeout: 30000 });
  console.log(`two runs: ${(await panel.locator(".compare-list li").allTextContents()).join(" | ")}`);
} catch (e) {
  failed = true;
  console.error(`FAILED: ${e.message}`);
}
const unexpected = refused.filter((x) => !/^403 \/api\/(sessions|experiments)/.test(x));
if (unexpected.length) errors.push(...unexpected.map((x) => `request refused: ${x}`));
if (errors.length) {
  failed = true;
  console.error(`console errors:\n  ${errors.join("\n  ")}`);
}
await browser.close();
process.exit(failed ? 1 : 0);
