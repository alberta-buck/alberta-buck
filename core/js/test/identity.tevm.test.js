// Phase 4 Stage 0: the full identity ceremony on Tevm, in-process.
//
// The wasm buck-identity kernel generates the registration NIZK and the
// REAL IdentityRegistry bytecode verifies it on-chain inside Tevm's EVM
// (BN254 add/mul/PAIRING precompiles) -- the capability the in-browser
// demo rests on.  Also pins the simulated-time controls
// (evm_setNextBlockTimestamp / evm_mine) the agent world needs.
//
// Skips cleanly when the wasm kernel or forge artifacts are not built.

import { test } from "node:test";
import assert from "node:assert/strict";

import { tevmSession, devAccount } from "../src/backends.js";
import { loadArtifact, artifactsAvailable } from "../src/nodefs.js";

let id = null;
try {
  id = await import("../src/identity.js");
} catch {
  // wasm not built
}
const skip = !id
  ? "kernel not built (make nix-core-build-wasm)"
  : !artifactsAvailable()
    ? "forge artifacts not built (make nix-build)"
    : false;

// BN254.sol struct components are UPPERCASE X/Y at the ABI.
const g = (p) => ({ X: p.x, Y: p.y });
const ct = (E) => ({ R: g(E.R), C: g(E.C) });
const g2 = (p) => ({ X: [p.x[0], p.x[1]], Y: [p.y[0], p.y[1]] });

test("identity ceremony: wasm NIZK verified by IdentityRegistry on tevm", { skip }, async () => {
  // Deterministic scalars (an LCG over Fr; live use draws WebCrypto).
  let seed = 0xa1bcb0can;
  const rand = () => {
    seed = (seed * 6364136223846793005n + 1442695040888963407n) & ((1n << 256n) - 1n);
    const v = seed % id.ORDER;
    return v === 0n ? 1n : v;
  };

  const session = await tevmSession();
  const gas = 15_000_000n;
  const reg = await session.deploy(loadArtifact("IdentityRegistry"),
    [session.account.address], { name: "IdentityRegistry", gas });

  // Issuer: PS keypair from the kernel, trusted on-chain.
  const [skX, skY] = [rand(), rand()];
  const pkX = id.g2Mul(id.G2, skX);
  const pkY = id.g2Mul(id.G2, skY);
  const issuerAddr = "0x" + "15".repeat(20);
  await session.send(reg, "trustIssuer",
    [issuerAddr, { X: g2(pkX), Y: g2(pkY) }], { tag: "trustIssuer" });

  // Wallet-side ceremony, unicode identity (the canonical-dialect pin).
  const fields = {
    given_name: "Chloé", family_name: "Bélanger-李",
    jurisdiction: "Alberta, Canada", id_type: "Alberta Identity Card",
    id_number: "AIC-2026-0000001", date_of_birth: "1994-11-02",
    issuer_id: "atb-financial-ca", issued_at: "2026-07-03T00:00:00Z", epoch: 42,
  };
  const m = id.identityScalar(id.canonicalIdentity(fields));
  const sigmaP = id.psRerandomize(id.psSign(skX, skY, m, rand()), rand());
  const sk = rand();
  const pk = id.g1Mul(id.G1, sk);
  const r = rand();
  const E = id.elgamalEncrypt(id.g1Mul(id.G1, m), pk, r);
  const registrant = BigInt(session.account.address);
  const proof = id.registrationProve(sigmaP, m, r, pk, E, registrant, rand(), rand());
  assert.ok(id.registrationVerify(sigmaP, E, pk, pkX, pkY, proof, registrant),
    "kernel-side verify");

  const args = [
    issuerAddr, g(pk), ct(E),
    { sigma_1: g(sigmaP.sigma_1), sigma_2: g(sigmaP.sigma_2) },
    { e: proof.e, s_m: proof.s_m, s_r: proof.s_r,
      A_ps: g(proof.A_ps), T_C: g(proof.T_C), T_R: g(proof.T_R) },
  ];

  // THE moment: the real Solidity verifier accepts the wasm proof
  // in-process (exercises tevm's bn254 pairing precompile).
  await session.send(reg, "register", args, { tag: "register", gas: 3_000_000n });
  assert.equal(await session.call(reg, "isVerified", [session.account.address]), true);

  // Replay from another account must revert (Fiat-Shamir binds registrant).
  await session.send(reg, "register", args,
    { tag: "register:replay", gas: 3_000_000n, account: devAccount(1),
      expect: "revert" });
  assert.equal(session.mismatches.length, 0, "declared-revert matched");

  // Simulated time: jump a day and mine -- the agent world's clock.
  const before = (await session.client.getBlock()).timestamp;
  await session.client.request({
    method: "evm_setNextBlockTimestamp", params: [Number(before) + 86_400] });
  await session.client.request({ method: "evm_mine", params: [] });
  const after = (await session.client.getBlock()).timestamp;
  assert.equal(after - before, 86_400n);
});
