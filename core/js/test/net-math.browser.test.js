// Exercise the web-target WASM and the shipped BigInt wrapper in Chromium.
// No EVM or substitute monetary implementation is used in this math check.
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { chromium } from "playwright-core";

const web = new URL("../kernel/web/buck_math.js", import.meta.url);
const wasm = new URL("../kernel/web/buck_math_bg.wasm", import.meta.url);
const skip = !existsSync(web) || !existsSync(wasm) ? "web math kernel not built (make nix-core-build-wasm-web)"
  : !process.env.PLAYWRIGHT_BROWSERS_PATH ? "no browser (run inside the Nix shell)" : false;
if (skip && process.env.BUCK_REQUIRE_NET_MATH_BROWSER === "1") throw new Error(skip);

test("Chromium replays canonical uint256 vectors through the web WASM entry point", { skip }, async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    const count = await page.evaluate(async ({ source, wrapper, bytes, vectors }) => {
      const load = (text) => import(`data:text/javascript;charset=utf-8,${encodeURIComponent(text)}`);
      const [kernel, { createNetMath }] = await Promise.all([load(source), load(wrapper)]);
      await kernel.default({ module_or_path: Uint8Array.from(atob(bytes), (c) => c.charCodeAt(0)) });
      const math = createNetMath(kernel);
      for (const row of vectors.cases) {
        let args = row.args.map(BigInt);
        if (row.op === "mulDiv") args.push(row.roundUp);
        if (row.op === "quoteAtTick") args = [Number(args[0] - (1n << 23n)), args[1], args[2] === 1n];
        try {
          const value = math[row.op](...args);
          if (row.error || value !== BigInt(row.value)) throw new Error(`mismatch: ${row.name}`);
        } catch (err) {
          if (!row.error || err.message !== row.error) throw new Error(`${row.name}: ${err.message}`);
        }
      }
      return vectors.cases.length;
    }, {
      source: readFileSync(web, "utf8"),
      wrapper: readFileSync(new URL("../src/net-math.js", import.meta.url), "utf8"),
      bytes: readFileSync(wasm).toString("base64"),
      vectors: JSON.parse(readFileSync(new URL("../../../test/vectors/net-math-vectors.json", import.meta.url))),
    });
    assert.ok(count >= 300);
  } finally {
    await browser.close();
  }
});
