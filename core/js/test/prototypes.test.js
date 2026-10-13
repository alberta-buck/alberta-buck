// The prototype agents (core/js/prototypes/) are structural goals banked
// ahead of the world that runs them (Stage 6/7).  This gate keeps them
// honest until then: they must parse, export the agent contract
// (setup/act, plain constructor state, a ledger), and respect the
// doctrine's one-screen budget.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { BasketInvestor } from "../prototypes/basket-investor.js";
import { MortgageRetiree } from "../prototypes/mortgage-retiree.js";

const protoDir = join(dirname(new URL(import.meta.url).pathname),
                      "..", "prototypes");

// Generous: a screen and a half INCLUDING the doctrine header comments.
// If a prototype outgrows this, the plumbing is leaking into the agent.
// The farmer runs TWO debts, a revolving line, and an investing leg on
// the same chassis -- it gets an explicit larger allowance.
const LINE_BUDGETS = {
  "basket-investor.js": 140,
  "mortgage-retiree.js": 140,
  "farmer.js": 190,
};

test("prototypes: the agent contract shape", () => {
  const investor = new BasketInvestor({
    token: {}, amount: 10n ** 18n, account: { address: "0x0" } });
  const retiree = new MortgageRetiree({
    house: 400_000n * 10n ** 6n, mortgageBp: 550n, premiumBp: 50n,
    payment: 3_000n * 10n ** 6n, account: { address: "0x0" } });

  for (const agent of [investor, retiree]) {
    assert.equal(typeof agent.setup, "function", "setup(world)");
    assert.equal(typeof agent.act, "function", "act(world, day, tick)");
    assert.equal(agent.act.length, 3, "act takes (world, day, tick)");
    assert.ok(Array.isArray(agent.ledger), "analytics ledger is a plain array");
  }
  assert.equal(investor.holdDays, 180);
  assert.equal(retiree.hypoOwing, 400_000n * 10n ** 6n);
});

test("prototypes: the one-screen doctrine budget", () => {
  for (const [f, budget] of Object.entries(LINE_BUDGETS)) {
    const lines = readFileSync(join(protoDir, f), "utf8").split("\n").length;
    assert.ok(lines <= budget,
      `${f} is ${lines} lines (> ${budget}): plumbing is leaking ` +
      "into the agent -- move it into the world builder");
  }
});

test("prototypes: the debtor's mortgage arithmetic amortizes", async () => {
  // The debtor's off-chain arithmetic is pure and runnable today.  A
  // stub world reports no signals (empty credit state, bvib == 0), so
  // no BUCK legs fire: the actual and counterfactual mortgages must
  // amortize IDENTICALLY -- $400k at 5.5% against a $3k fixed payment
  // -- with zero cost telemetry (no mints, no pool crossings).
  const r = new MortgageRetiree({
    house: 400_000n * 10n ** 6n, mortgageBp: 550n, premiumBp: 50n,
    payment: 3_000n * 10n ** 6n, account: { address: "0x0" } });
  const zeroCredit = { drawn: 0n, limit: 0n, held: 0n, unactivated: 0n,
                       jub: 0n, spendable: 0n, capacity: 0n, headroom: 0n };
  const world = { session: { call: async () => 0n }, buck: {}, basket: {},
                  holderAddress: (a) => a.address,
                  creditState: async () => zeroCredit,
                  gateShortfall: async () => ({ required: 0n, shortfall: 0n }),
                  buyForGate: async () => ({ tranche: 0n, spend: 0n, got: 0n }),
                  findDepositCash: async () => 0n,
                  sellBite: async () => 0n,
                  mintTranche: async () => ({ ok: false, premium: 0n }),
                  sellBuckCapped: async () => ({ sold: 0n, got: 0n }),
                  unwindBite: async () => ({ spot: 10n ** 6n, capBuck: 0n }),
                  buyBuck: async () => 0n };

  // The debtor acts every day (its refinancing is paced by the market, not
  // the calendar); the payment and the ledger row fall monthly.
  for (let d = 0; d < 360; d++) await r.act(world, d, 0);
  const rows = r.ledger;
  assert.equal(rows.length, 12, "one ledger row a month");
  assert.ok(rows.every((row, i) => i === 0 || row.mortgageOwing < rows[i - 1].mortgageOwing),
    "principal must fall every month");
  assert.ok(rows.every((row) => row.mortgageOwing === row.hypoOwing),
    "with no BUCK legs, actual == counterfactual trajectory");
  // Interest is charged on the days just past: ~$397k at 5.5% accrues
  // ~$1,820 over the 30 days to the second payment, so the $3k payment
  // retires ~$1,180 of principal.
  const drop = rows[0].mortgageOwing - rows[1].mortgageOwing;
  assert.ok(drop > 1_150n * 10n ** 6n && drop < 1_200n * 10n ** 6n,
    `a month's payment retires ~$1,180 of principal (got ${drop})`);
  assert.ok(rows.every((row) => row.premiumPaid === 0n
                             && row.tradeLoss === 0n && row.jub === 0n),
    "zero cost telemetry when no legs fire");
  assert.equal(rows[0].banked, 0n, "the payment is fully consumed early on");
});
