#!/usr/bin/env node
// A screenshot of the dashboard as served (alberta_buck.sim.dashboard), in headless Chromium
// (the flake's), with the pointer over one chart so the crosshair and tooltip show. Prints any
// console error the page logged.
//
//   node dashboard/bin/screenshot.mjs 'http://127.0.0.1:8890/?run=<id>' out.png [light|dark] [width]
import { chromium } from "playwright-core";
const [url, out, scheme = "light", width = "1280"] = process.argv.slice(2);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: Number(width), height: 900 }, colorScheme: scheme });
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
page.on("pageerror", (e) => errors.push(String(e)));
await page.goto(url);
await page.waitForSelector(".card svg path.line", { timeout: 20000 });
await page.waitForTimeout(500);
await page.screenshot({ path: out, fullPage: true });
// A full-page capture resizes the viewport (and the pointer leaves), so the hover is captured
// separately: the second card, with the pointer over it, beside the page as <out>-hover.png.
const card = page.locator(".card").nth(1);
await card.scrollIntoViewIfNeeded();
const box = await page.locator(".card .plot").nth(1).boundingBox();
await page.mouse.move(box.x + box.width * 0.5, box.y + 100);
await page.mouse.move(box.x + box.width * 0.6, box.y + 100, { steps: 4 });
await page.waitForTimeout(200);
const tooltips = await page.locator(".tooltip:not([hidden])").count();
await card.screenshot({ path: out.replace(/\.png$/, "-hover.png") });
console.log(JSON.stringify({ errors, tooltips }));
await browser.close();
