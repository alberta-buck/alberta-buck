// Phase 4 Stage 1: the REAL BUCK stack on Tevm, wholly in JS.
//
// buildBuckWorld deploys IdentityRegistry + BuckCredit +
// BuckKControllerDirect + Buck (deploy.py's order); two agents onboard
// with REAL kernel-proved identities; a BuckCredit activates; an
// identity-gated transfer draws credit; simulated time makes demurrage
// visible -- and the on-chain feeOwing must agree BIT-EXACTLY with the
// buck-math wasm kernel's prediction.
//
// Skips cleanly when the wasm kernels or forge artifacts are not built.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

import { tevmSession, devAccount } from "../src/backends.js";
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

test("buckworld: onboard, activate credit, transfer, demurrage == kernel", { skip }, async () => {
  const { buildBuckWorld, onboard, createCredit, identityApprove, advanceTime, DAY } =
    await import("../src/buckworld.js");

  // Deterministic scalar stream (live use draws WebCrypto).
  let seed = 0xb0c4a11cen;
  const rng = () => {
    seed = (seed * 6364136223846793005n + 1442695040888963407n) & ((1n << 256n) - 1n);
    const v = seed % id.ORDER;
    return v === 0n ? 1n : v;
  };

  const session = await tevmSession();
  const world = await buildBuckWorld(session, loadArtifact, { rng });

  // Two real identities: the unicode payer and an ASCII counterparty.
  const alice = await onboard(world, session.account, {
    given_name: "Chloé", family_name: "Bélanger-李",
    jurisdiction: "Alberta, Canada", id_type: "Alberta Identity Card",
    id_number: "AIC-2026-0000001", date_of_birth: "1994-11-02",
    issued_at: "2026-07-03T00:00:00Z", epoch: 42,
  }, { rng });
  const bobAcct = devAccount(1);
  const bob = await onboard(world, bobAcct, {
    given_name: "Bob", family_name: "Smith",
    jurisdiction: "Alberta, Canada", id_type: "Corporate Registration",
    id_number: "AB-CORP-2026-00182", date_of_birth: "1985-07-22",
    issued_at: "2026-07-03T00:00:00Z", epoch: 42,
  }, { rng });
  assert.equal(await session.call(world.reg, "isVerified", [alice.account.address]), true);
  assert.equal(await session.call(world.reg, "isVerified", [bob.account.address]), true);

  // Alice activates a 1000-BUCK credit line: creditLimit = face * K0 = 750.
  const FACE = 1_000_000000n;                    // BUCK is 6 dp
  await createCredit(world, alice, FACE);
  assert.equal(await session.call(world.buck, "creditLimit", [alice.account.address]),
    (FACE * 750_000_000_000_000_000n) / 10n ** 18n);

  // Identity-gated transfer: paying an UNVERIFIED address reverts...
  const stranger = devAccount(2).address;
  await session.send(world.buck, "transfer", [stranger, 250_000000n],
    { tag: "pay:unverified", expect: "revert", gas: 1_000_000n });
  assert.equal(session.mismatches.length, 0);
  assert.equal(session.lastRevertReason, "BUCK: recipient not verified");

  // Private<->private payment needs the BILATERAL identity-approve
  // handshake first: without it, the transfer is refused...
  await session.send(world.buck, "transfer", [bob.account.address, 250_000000n],
    { tag: "pay:unapproved", expect: "revert", gas: 1_000_000n });
  assert.equal(session.lastRevertReason, "BUCK: sender must identity-approve recipient");

  // ...so each party re-encrypts their registered identity for the other
  // (Chaum-Pedersen proved on-chain; Bob can now DECRYPT Alice's M).
  const eForBob = await identityApprove(world, alice, bob, { rng });
  await identityApprove(world, bob, alice, { rng });
  assert.deepEqual(id.elgamalDecrypt(eForBob, bob.kp.sk), alice.M,
    "bilateral disclosure: Bob recovers Alice's identity point");

  // Paying Bob now draws Alice's credit (signed balance goes negative:
  // an outstanding claim on her own assets, not debt).
  const t0 = (await session.client.getBlock()).timestamp;
  await session.send(world.buck, "transfer", [bob.account.address, 250_000000n],
    { tag: "pay:bob", gas: 1_000_000n });
  assert.equal(await session.call(world.buck, "signedBalanceOf", [alice.account.address]),
    -250_000000n);
  assert.equal(await session.call(world.buck, "balanceOf", [bob.account.address]),
    250_000000n);

  // 30 simulated days: demurrage accrues on Bob's positive balance, and
  // the chain's feeOwing agrees BIT-EXACTLY with the buck-math kernel
  // (fresh receipt: buckSeconds 0 at t0, raw 250 BUCK, elapsed t1-t0).
  const t1 = await advanceTime(world, 30 * DAY);
  const elapsed = t1 - t0;
  const chainFee = await session.call(world.buck, "feeOwing", [bob.account.address]);
  const kernelFee = math.fee_owing(0n, 250_000000n, elapsed);
  assert.equal(chainFee, kernelFee, "chain demurrage != kernel prediction");
  assert.ok(chainFee > 0n, "30 days must accrue a visible fee");

  // Alice holds drawn credit (raw <= 0): no demurrage, per the public
  // feeOwing gate -- kernel and chain agree there too.
  assert.equal(await session.call(world.buck, "feeOwing", [alice.account.address]),
    math.fee_owing(0n, -250_000000n, elapsed));

  // The whole run journaled clean: every declared expectation matched.
  assert.equal(session.mismatches.length, 0);
});
