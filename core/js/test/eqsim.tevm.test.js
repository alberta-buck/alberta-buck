// Stage 6/7 gate: the minimal two-agent equilibrium LOOP runs on Tevm.
//
// DayClock + PinWhale (TOKEN/USDC pinned to the seeded walk) + ParityArb
// (triangle consistency) + WheelKeeper + PidKeeper + BasketSaver +
// MonthlyIncome + MortgageRetiree (the prototype, live):
//
//   * every op matches its declared expectation,
//   * the arb holds TOKEN/BUCK to the implied triangle price,
//   * K stays interior (the controller neither rails nor explodes),
//   * basketValueInBuck stays in a sane band around parity,
//   * the saver completes a full deposit -> term -> redeem cycle,
//   * the debtor books monthly ledger rows (both chart columns).
//
// Twice.  On the equity basket with the basket machine, the world's
// default, for 14 days: the wheel pairs the saver's deposit and the exit is
// paid in BUCK.  A wheel turn is over a million gas, about four seconds on
// tevm, so a day there costs some twenty seconds and the run is kept short.
// And on the pro-rata basket for 35 days, as before (~2 min), which also
// takes the debtor through its first month.

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

async function loop({ basket, days, holdDays }) {
  const { buildEquilibriumWorld, DayClock, PidKeeper, MonthlyIncome, WheelKeeper } =
    await import("../src/scenarios/eqworld.js");
  const { PinWhale } = await import("../src/agents/whale.js");
  const { ParityArb } = await import("../src/agents/arb.js");
  const { BasketSaver } = await import("../src/agents/saver.js");
  const { MortgageRetiree } = await import("../prototypes/mortgage-retiree.js");
  const { runDays } = await import("../src/world.js");

  const session = await tevmSession();
  const world = await buildEquilibriumWorld(session, loadArtifact, {
    identity: id.default, basket,
    urArtifact: JSON.parse(readFileSync(urPath, "utf8")),
    feedSeed: 0xF005, stepBp: 120 });

  const HOUSE = 400_000n * 10n ** 6n;
  const PAYMENT = 3_000n * 10n ** 6n;
  const debtorAcct = privateKeyToAccount(generatePrivateKey());
  const saver = new BasketSaver({
    account: privateKeyToAccount(generatePrivateKey()), holdDays });
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
    arb, new WheelKeeper({ arb }), new PidKeeper(), saver,
    new MonthlyIncome({ account: debtorAcct, amount: PAYMENT }),
    debtor,
  ], { days, onDay: (d) => world.record(d) });

  // Every op matched its declared expectation.
  assert.equal(session.mismatches.length, 0,
    `mismatches: ${JSON.stringify(session.mismatches[0] ?? null)}`);
  assert.equal(world.series.length, days);

  // The arb holds the triangle together at the end of the run: within
  // its own 150bp, and the 50bp the debtor may have moved the BUCK/dollar
  // pool since (it acts after the arb, and in this $10M market it is still
  // moving its loan onto its credit, a day's bite at a time).
  const [pTU, pUB, pTB] = await Promise.all(
    [world.spotUsd(0), world.spotUB(), world.spotBuck(0)]);
  const implied = (pTU * 1_000_000n) / pUB;
  const divBp = ((pTB - implied) * 10_000n) / implied;
  assert.ok(divBp > -200n && divBp < 200n,
    `TOKEN/BUCK within 200bp of the implied price (got ${divBp}bp, ` +
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
  assert.ok(debtor.ledger.every((r) => r.hypoOwing <= HOUSE),
    "counterfactual amortizes");

  console.log(`eqsim (${world.basketKind}, ${days}d): K=${K.toFixed(4)} bvib=${bvib.toFixed(4)} ` +
    `div=${divBp}bp triangles=${arb.triangles} ` +
    `saver=${JSON.stringify(events)} ` +
    `debtor drawn=${debtor.ledger.at(-1).drawn} ` +
    `mortgage=${debtor.mortgageOwing}`);
  return { world, session, saver, debtor };
}

test("eqsim: the two-agent equilibrium loop runs on tevm, on the basket machine", { skip }, async () => {
  const { world, session, saver, debtor } = await loop({ basket: "machine", days: 14, holdDays: 8 });
  assert.equal(world.basketKind, "machine", "the machine is the world's default basket");
  // The wheel paired what was deposited: only dust waits in the wallet
  // (fees a position collected since the last turn), far under a
  // thousandth of the book.
  for (let i = 0; i < world.tokens.length; i++) {
    const t = world.tokens[i];
    const idle = await session.call(world.basket, "idleToken", [BigInt(i)]);
    assert.ok(idle * 1000n < t.seedTok,
      `${t.sym}: nothing but dust waits to be paired (${idle} of ${t.seedTok})`);
  }
  // The exit was paid in BUCK, and the saver holds it.
  const got = saver.ledger.find((r) => r.event === "redeem").usd;
  assert.ok(got > 0n, "the saver was paid");
  assert.ok(await session.call(world.buck, "balanceOf", [saver.account.address]) > 0n,
    "in BUCK");
  assert.equal(debtor.ledger.length, 1, "debtor booked d0");
});

test("eqsim: the same loop on the pro-rata basket, through the debtor's first month", { skip }, async () => {
  const { world, debtor } = await loop({ basket: "prorata", days: 35, holdDays: 20 });
  assert.equal(world.wheel, null, "the pro-rata basket has no wheel");
  assert.equal(debtor.ledger.length, 2, "debtor booked d0 and d30");
});
