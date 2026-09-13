// The Phase 3 in-browser acceptance, smoke-tested headlessly: the SAME
// web-target wasm artifacts the demo page (demo/identity-proofs.html)
// loads must generate and verify the full registration ceremony.  The
// web-target `init()` accepts wasm bytes directly, so node exercises the
// exact browser bundle.
//
// Build first:  make nix-core-build-wasm-web

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

let wasm = null;
try {
  wasm = await import("alberta-buck-kernel/web/identity");
  const bytes = readFileSync(
    fileURLToPath(new URL("../kernel/web/buck_identity_bg.wasm", import.meta.url)));
  await wasm.default({ module_or_path: bytes });
} catch {
  wasm = null;
}
const skip = wasm ? false : "web bundle not built (make nix-core-build-wasm-web)";

test("browser bundle: full registration ceremony proves + verifies", { skip }, () => {
  const ORDER = BigInt(wasm.order());
  const hex = (v) => "0x" + BigInt(v).toString(16).padStart(64, "0");
  let seed = 0xdeadbeefn;
  const rand = () => {
    // deterministic LCG over Fr for the smoke (the page uses WebCrypto)
    seed = (seed * 6364136223846793005n + 1442695040888963407n) & ((1n << 256n) - 1n);
    const v = seed % ORDER;
    return v === 0n ? 1n : v;
  };

  const canon = '{"epoch":42,"given_name":"Alice"}';
  const m = BigInt(wasm.identity_scalar(canon));

  const [skX, skY] = [rand(), rand()];
  const G2 = wasm.g2_generator();
  const pkX = wasm.g2_mul(...G2, hex(skX));
  const pkY = wasm.g2_mul(...G2, hex(skY));
  const sigma = wasm.ps_sign(hex(skX), hex(skY), hex(m), hex(rand()));
  assert.ok(wasm.ps_verify(pkX, pkY, ...sigma, hex(m)));

  const sigmaP = wasm.ps_rerandomize(...sigma, hex(rand()));
  const sk = rand();
  const pk = wasm.g1_mul("0x1", "0x2", hex(sk));
  const M = wasm.g1_mul("0x1", "0x2", hex(m));
  const r = rand();
  const E = wasm.elgamal_encrypt(...M, ...pk, hex(r));

  const registrant = "0x" + "a11ce".padStart(40, "0");
  const chainid = "0x1";
  const proof = wasm.registration_prove(
    sigmaP, hex(m), hex(r), ...pk, E, registrant, hex(sk), chainid,
    hex(rand()), hex(rand()), hex(rand()));
  assert.ok(wasm.registration_verify(sigmaP, E, ...pk, pkX, pkY, proof, registrant, chainid));
  assert.ok(!wasm.registration_verify(
    sigmaP, E, ...pk, pkX, pkY, proof, "0x" + "bad".padStart(40, "0"), chainid));
});
