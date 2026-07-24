// Phase 4 Stage 2: the SHIPPED browser bundle, executed headlessly.
//
// Imports demo/app.js -- the exact esbuild output buckworld.html loads --
// under a ~40-line DOM stub, with fetch() redirected to the on-disk
// wasm-web/ files the page would serve.  The demo's scripted opening
// story (two citizens onboarded through the kernel ceremony, a credit
// line, the bilateral handshake, a payment, 30 simulated days) must run
// to completion and light the bit-exact demurrage badge green.
//
// Skips cleanly until `make nix-core-demo-buckworld` has built the
// bundle and the web-target kernels.

import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const demoDir = join(dirname(fileURLToPath(import.meta.url)), "..", "demo");
const bundle = join(demoDir, "app.js");
const skip = existsSync(bundle) && existsSync(join(demoDir, "wasm-web", "buck_identity_bg.wasm"))
  ? false
  : "demo bundle not built (make nix-core-demo-buckworld)";

// ---- minimal DOM: just enough surface for demo/src/main.js -----------------

class El {
  constructor() {
    this.children = [];
    this.options = [];
    this.value = "";
    this.className = "";
    this.textContent = "";
    this.selectedIndex = 0;
    this.disabled = false;
    this.onclick = null;
    this.style = {};
    this._html = "";
  }
  set innerHTML(v) { this._html = v; this.children = []; this.options = []; }
  get innerHTML() { return this._html; }
  appendChild(c) { this.children.push(c); this.options.push(c); return c; }
  prepend(c) { this.children.unshift(c); return c; }
}

test("shipped bundle: the opening story runs headless, badge bit-exact", { skip }, async () => {
  const ids = new Map();
  const byId = (id) => {
    if (!ids.has(id)) ids.set(id, new El());
    return ids.get(id);
  };
  globalThis.document = {
    getElementById: byId,
    createElement: () => new El(),
    querySelectorAll: () => [],
  };
  // The page fetches its wasm relative to the demo dir; serve from disk.
  globalThis.fetch = async (path) =>
    new Response(readFileSync(join(demoDir, String(path))),
      { headers: { "content-type": "application/wasm" } });

  await import(bundle);

  // main() is async fire-and-forget; poll the badge it sets last.
  const deadline = Date.now() + 60_000;
  while (byId("badge").textContent === "" && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 250));
    if (byId("status").textContent.startsWith("FAILED")) {
      assert.fail(byId("status").textContent);
    }
  }

  assert.equal(byId("badge").className, "ok", byId("badge").textContent);
  assert.match(byId("badge").textContent, /bit-exact/);
  // The roster rendered both opening citizens (unicode name included).
  const names = byId("roster").children.map((c) => c.innerHTML).join("\n");
  assert.match(names, /Chloé Bélanger-李/);
  assert.match(names, /Bob Smith/);
  // The journal streamed the story into the page.
  assert.ok(byId("log").children.length > 10, "journal panel populated");

  // Stage 3 through the SAME shipped bundle: open the market via the
  // page's own (stubbed) controls and tick two simulated days -- the
  // whale + trader run in the background and the panel renders.
  await byId("openmkt").onclick();
  await byId("mtick").onclick();
  await byId("mtick").onclick();
  assert.match(byId("market").innerHTML, /simulated day 2/);
  assert.match(byId("market").innerHTML, /pool spot/);
});
