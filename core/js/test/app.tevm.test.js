// Phase 4 Stage 2: the demo CONTROLLER end-to-end on Tevm.
//
// The browser page (demo/buckworld.html) drives BuckWorldApp with
// web-loaded dependencies; this gate drives the SAME class with
// node-loaded ones -- fresh funded citizen accounts, real registrations,
// the bilateral approve handshake, payments (including the protocol's
// refusals surfacing as {ok:false, reason}), time jumps, and the
// fresh-receipt demurrage prediction agreeing with the chain.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

import { tevmSession } from "../src/backends.js";
import { loadArtifact, artifactsAvailable } from "../src/nodefs.js";

const require = createRequire(import.meta.url);
let id = null;
let math = null;
try {
  id = await import("../src/identity.js");
  math = require("../wasm/buck_math.js");
} catch {
  // kernels not built
}
const skip = !id || !math
  ? "kernels not built (make nix-core-build-wasm)"
  : !artifactsAvailable()
    ? "forge artifacts not built (make nix-build)"
    : false;

test("demo controller: citizens, handshake, payments, time, demurrage", { skip }, async () => {
  const { BuckWorldApp, SAMPLE_CITIZENS } = await import("../demo/src/app.js");

  let seed = 0xdecafbadn;
  const rng = () => {
    seed = (seed * 6364136223846793005n + 1442695040888963407n) & ((1n << 256n) - 1n);
    const v = seed % id.ORDER;
    return v === 0n ? 1n : v;
  };

  const app = new BuckWorldApp({
    session: await tevmSession(),
    identity: id.default,
    artifacts: loadArtifact,
    makeAccount: () => privateKeyToAccount(generatePrivateKey()),
    rng,
    math,
  });
  await app.boot();

  // Three citizens off the sample roster (unicode included).
  const c0 = await app.addCitizen(SAMPLE_CITIZENS[0]);
  const c1 = await app.addCitizen(SAMPLE_CITIZENS[1]);
  const c2 = await app.addCitizen(SAMPLE_CITIZENS[2]);
  assert.equal(app.citizens.length, 3);

  // Chloé insures a 1000-BUCK credit line.
  await app.credit(c0, 1_000_000000n);

  // Paying without the handshake is REFUSED, with the protocol's reason.
  const refused = await app.pay(c0, c1, 100_000000n);
  assert.equal(refused.ok, false);
  assert.match(refused.reason, /identity-approve/);

  // Handshake, then the payment draws Chloé's credit.
  await app.approvePair(c0, c1);
  const paid = await app.pay(c0, c1, 250_000000n);
  assert.equal(paid.ok, true);

  const t0 = (await app.session.client.getBlock()).timestamp;
  await app.jump(30 * BuckWorldApp.DAY);
  const snap = await app.snapshot();
  const elapsed = snap.clock - t0;

  const sChloe = snap.citizens.find((c) => c.name === c0.name);
  const sBob = snap.citizens.find((c) => c.name === c1.name);
  const sZoe = snap.citizens.find((c) => c.name === c2.name);
  assert.equal(sChloe.signed, -250_000000n);
  assert.equal(sZoe.balance, 0n);
  assert.equal(sChloe.creditLimit, 750_000000n);

  // Demurrage is LIVE in balanceOf: Bob's spendable balance is his
  // receipt net of the accrued fee -- and the fee agrees BIT-EXACTLY
  // with the buck-math kernel's fresh-receipt prediction.
  assert.ok(sBob.feeOwing > 0n);
  assert.equal(sBob.feeOwing, app.predictFee(250_000000n, elapsed));
  assert.equal(sBob.balance, 250_000000n - sBob.feeOwing);
  assert.equal(sChloe.feeOwing, 0n);

  // The journal recorded the story, including the one declared-ok revert
  // (the refused payment) as the run's single mismatch.
  assert.equal(app.session.mismatches.length, 1);
});
