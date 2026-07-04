// The SHIPPED equilibrium bundle, executed headlessly: demo/eqapp.js --
// the exact esbuild output eqworld.html loads -- under the same tiny
// DOM stub as demo.bundle.test.js, fetch() redirected to the on-disk
// wasm.  The opening cast (one saver through the full ceremony, one
// debtor through its proxy, two ticked days) must come up live; then
// the page's own controls add a saver and a debtor and step a day.
//
// Skips cleanly until `make nix-core-demo-eqworld` has built the bundle.

import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const demoDir = join(dirname(fileURLToPath(import.meta.url)), "..", "demo");
const bundle = join(demoDir, "eqapp.js");
const skip = existsSync(bundle)
  && existsSync(join(demoDir, "wasm-web", "buck_identity_bg.wasm"))
  ? false
  : "eq demo bundle not built (make nix-core-demo-eqworld)";

class El {
  constructor() {
    this.children = [];
    this.options = [];
    this.value = "";
    this.className = "";
    this.textContent = "";
    this.disabled = false;
    this.onclick = null;
    this.style = {};
    this._html = "";
  }
  set innerHTML(v) { this._html = v; this.children = []; this.options = []; }
  get innerHTML() { return this._html; }
  appendChild(c) { this.children.push(c); return c; }
  prepend(c) { this.children.unshift(c); return c; }
}

test("shipped eq bundle: boots live, adds agents, steps a day", { skip }, async () => {
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
  globalThis.fetch = async (path) =>
    new Response(readFileSync(join(demoDir, String(path))),
      { headers: { "content-type": "application/wasm" } });

  // Inputs the page reads for the add controls.
  byId("sbudget").value = "25000";
  byId("shold").value = "60";
  byId("dhouse").value = "400000";
  byId("dpay").value = "3000";
  byId("speed").value = "1500";

  await import(bundle);

  // main() is async fire-and-forget; poll until the opening cast is live.
  const deadline = Date.now() + 180_000;
  while (!byId("status").textContent.startsWith("live") && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 250));
    if (byId("status").textContent.startsWith("FAILED")) {
      assert.fail(byId("status").textContent);
    }
  }
  assert.match(byId("status").textContent, /^live/,
    "opening cast came up live");

  // The opening state: day 2, both agents on the roster, charts rendered.
  assert.match(byId("statusbar").textContent, /day 2/);
  assert.equal(byId("roster").children.length, 2);
  assert.match(byId("charts").innerHTML, /<svg/);
  assert.match(byId("charts").innerHTML, /basketValueInBuck/);
  assert.ok(byId("log").children.length > 10, "journal panel populated");

  // Drive the page's own controls: add one of each, step a day.
  await byId("addsaver").onclick();
  await byId("adddebtor").onclick();
  assert.equal(byId("roster").children.length, 4, "dynamic adds landed");
  await byId("step").onclick();
  await new Promise((r) => setTimeout(r, 50));   // tickOnce refresh settles
  assert.match(byId("statusbar").textContent, /day 3/);
});
