// The browser loader for the wallet + registry kernels (wallet-web.js),
// smoke-tested headlessly on the SAME web-target wasm the pages load: it
// replays the canonical-dialect and envelope vectors, and shares one wasm
// instance with loadIdentity (identity-web.js) in either order.
//
// Build first:  make nix-core-build-wasm-web

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

let bytes = null;
try {
  await import("alberta-buck-kernel/web/identity");
  bytes = readFileSync(
    fileURLToPath(new URL("../kernel/web/buck_identity_bg.wasm", import.meta.url)));
} catch {
  bytes = null;
}
const skip = bytes ? false : "web bundle not built (make nix-core-build-wasm-web)";

const WV = JSON.parse(readFileSync(
  fileURLToPath(new URL("../../vectors/wallet-kernel-vectors.json", import.meta.url)),
  "utf8"));

test("browser wallet kernel: vectors, one wasm shared with the identity kernel", { skip }, async () => {
  const { loadWallet } = await import("../src/wallet-web.js");
  const { loadIdentity } = await import("../src/identity-web.js");
  const w = await loadWallet(bytes);
  const id = await loadIdentity();      // already initialized: no source needed

  for (const row of WV.canonical_json) {
    assert.equal(w.canonicalJson(row.input), row.canonical);
  }
  for (const row of WV.envelope) {
    assert.equal(w.receiptId(new TextEncoder().encode(row.canonical)), row.id12);
  }
  assert.equal(typeof w.registry.schnorrSign, "function");
  assert.equal(id.identityScalar('{"epoch":42,"given_name":"Alice"}'),
    (await loadIdentity(bytes)).identityScalar('{"epoch":42,"given_name":"Alice"}'));
});
