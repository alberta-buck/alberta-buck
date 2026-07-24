// Stage 6/7 gate: the minimal two-agent equilibrium LOOP runs on Tevm.
//
// DayClock + PinWhale (TOKEN/USDC pinned to the seeded walk) + ParityArb
// (triangle consistency) + PidKeeper + BasketSaver + MonthlyIncome +
// MortgageRetiree (the prototype, live) for 35 simulated days:
//
//   * every op matches its declared expectation,
//   * the arb holds TOKEN/BUCK to the implied triangle price,
//   * K stays interior (the controller neither rails nor explodes),
//   * basketValueInBuck stays in a sane band around parity,
//   * the saver completes a full deposit -> term -> redeem cycle,
//   * the debtor books monthly ledger rows (both chart columns).
//
// ~35 sim-days of real txs on tevm: the suite's heaviest gate (~2 min).

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";

import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

import { tevmSession } from "../src/backends.js";
import { loadArtifact, artifactsAvailable, repoRoot } from "../src/nodefs.js";

let id = null;
try {
  id = await import("../src/identity.js");
} catch {
  // kernel not built
}
const urPath = (() => {
  try {
    return join(repoRoot(), "alberta_buck", "sim", "artifacts",
                "UniversalRouter.json");
  } catch { return null; }
})();
const skip = !id ? "identity kernel not built (make nix-core-build-wasm)"
  : !artifactsAvailable() ? "forge artifacts not built (make nix-build)"
  : !(urPath && existsSync(urPath)) ? "vendored UR artifact missing"
  : false;

const DAYS = 35;

test("eqsim: the two-agent equilibrium loop runs on tevm", { skip }, async () => {
  const { buildEquilibriumWorld, DayClock, PidKeeper, MonthlyIncome } =
    await import("../src/scenarios/eqworld.js");
  const { PinWhale } = await import("../src/agents/whale.js");
  const { ParityArb } = await import("../src/agents/arb.js");
  const { BasketSaver } = await import("../src/agents/saver.js");
  const { MortgageRetiree } = await import("../prototypes/mortgage-retiree.js");
  const { runDays } = await import("../src/world.js");

  const session = await tevmSession();
  const world = await buildEquilibriumWorld(session, loadArtifact, {
    identity: id.default,
    urArtifact: JSON.parse(readFileSync(urPath, "utf8")),
    feedSeed: 0xF005, stepBp: 120 });

  const HOUSE = 400_000n * 10n ** 6n;
  const PAYMENT = 3_000n * 10n ** 6n;
  const debtorAcct = privateKeyToAccount(generatePrivateKey());
  const saver = new BasketSaver({
    account: privateKeyToAccount(generatePrivateKey()), holdDays: 20 });
  const debtor = new MortgageRetiree({
    house: HOUSE, mortgageBp: 550n, premiumBp: 50n, payment: PAYMENT,
    account: debtorAcct });
  const arb = new ParityArb();

  await runDays(world, [
    new DayClock(),
    ...world.tokens.map((t, i) => new PinWhale({
      simlp: world.simlp, pool: t.poolUsdc, token: t.erc20.address,
      tokenDec: t.dec, quote: world.usdc.address,
      refPrice: (d) => world.feeds[i][d % world.feeds[i].length] })),
    arb, new PidKeeper(), saver,
    new MonthlyIncome({ account: debtorAcct, amount: PAYMENT }),
    debtor,
  ], { days: DAYS, onDay: (d) => world.record(d) });

  // Every op matched its declared expectation.
  assert.equal(session.mismatches.length, 0,
    `mismatches: ${JSON.stringify(session.mismatches[0] ?? null)}`);
  assert.equal(world.series.length, DAYS);

  // The arb holds the triangle together at the end of the run.
  const [pTU, pUB, pTB] = await Promise.all(
    [world.spotUsd(0), world.spotUB(), world.spotBuck(0)]);
  const implied = (pTU * 1_000_000n) / pUB;
  const divBp = ((pTB - implied) * 10_000n) / implied;
  assert.ok(divBp > -150n && divBp < 150n,
    `TOKEN/BUCK within 150bp of the implied price (got ${divBp}bp, ` +
    `${arb.triangles} triangles)`);

  // The controller stays interior and the peg observable stays sane.
  const last = world.series[world.series.length - 1];
  const K = Number(last.K) / 1e18;
  const bvib = Number(last.bvib) / 1e18;
  assert.ok(K > 0.40 && K < 0.95, `K interior (got ${K})`);
  assert.ok(bvib > 0.70 && bvib < 1.30, `bvib sane (got ${bvib})`);

  // The saver completed a full cycle; the debtor kept its books.
  const events = saver.ledger.filter((r) => r.event !== "mark")
                             .map((r) => r.event);
  assert.deepEqual(events.slice(0, 2), ["deposit", "redeem"],
    `saver cycle (got ${events})`);
  assert.ok(saver.dollarDays > 0n, "dollar-days accounting ran");
  assert.equal(debtor.ledger.length, 2, "debtor booked d0 and d30");
  assert.ok(debtor.ledger.every((r) => r.hypoOwing <= HOUSE),
    "counterfactual amortizes");

  console.log(`eqsim: K=${K.toFixed(4)} bvib=${bvib.toFixed(4)} ` +
    `div=${divBp}bp triangles=${arb.triangles} ` +
    `saver=${JSON.stringify(events)} ` +
    `debtor drawn=${debtor.ledger[1].drawn} ` +
    `mortgage=${debtor.mortgageOwing}`);
});
