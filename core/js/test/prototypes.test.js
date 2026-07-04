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
const LINE_BUDGET = 140;

test("prototypes: the agent contract shape", () => {
  const investor = new BasketInvestor({
    token: {}, amount: 10n ** 18n, account: { address: "0x0" } });
  const retiree = new MortgageRetiree({
    house: 400_000n * 10n ** 6n, mortgageBp: 550n, premiumBp: 50n,
    income: 3_000n * 10n ** 6n, account: { address: "0x0" } });

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
  for (const f of ["basket-investor.js", "mortgage-retiree.js"]) {
    const lines = readFileSync(join(protoDir, f), "utf8").split("\n").length;
    assert.ok(lines <= LINE_BUDGET,
      `${f} is ${lines} lines (> ${LINE_BUDGET}): plumbing is leaking ` +
      "into the agent -- move it into the world builder");
  }
});

test("prototypes: the counterfactual mortgage amortizes", async () => {
  // The retiree's off-chain arithmetic is pure and runnable today.  A
  // stub world reports a settled position (drawn == 0), so act() books
  // only the insurer's bill and the counterfactual mortgage column:
  // $400k at 5.5% against $3k/month income must amortize.
  const r = new MortgageRetiree({
    house: 400_000n * 10n ** 6n, mortgageBp: 550n, premiumBp: 50n,
    income: 3_000n * 10n ** 6n, account: { address: "0x0" } });
  const world = { session: { call: async () => 0n }, buck: {}, basket: {} };

  for (let m = 0; m < 12; m++) {
    const owing = r.hypoOwing;
    await r.act(world, m * 30, 0);
    assert.ok(r.hypoOwing < owing, "principal must fall every month");
  }
  const interest = r.ledger.map((row) => row.mortgageUsd);
  assert.ok(interest[11] < interest[0], "interest declines as principal retires");
  assert.ok(interest[0] > 1_800n * 10n ** 6n && interest[0] < 1_850n * 10n ** 6n,
    "400k at 5.5%/12 is ~$1,833 first-month interest");
  // The insurer's bill books every month, even while the position idles.
  const premium = 400_000n * 10n ** 6n * 50n / 10_000n / 12n;
  assert.ok(r.ledger.every((row) => row.buckUsd === premium),
    "buckUsd column carries exactly the premium when no buys fire");
});
