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

const protoDir = join(dirname(new URL(import.meta.url).pathname),
                      "..", "prototypes");

// Generous: a screen and a half INCLUDING the doctrine header comments.
// If a prototype outgrows this, the plumbing is leaking into the agent.
// The farmer runs TWO debts, a revolving line, and an investing leg on
// the same chassis -- it gets an explicit larger allowance.
const LINE_BUDGETS = {
  "basket-investor.js": 140,
  "farmer.js": 190,
};

test("prototypes: the agent contract shape", () => {
  const investor = new BasketInvestor({
    token: {}, amount: 10n ** 18n, account: { address: "0x0" } });
  for (const agent of [investor]) {
    assert.equal(typeof agent.setup, "function", "setup(world)");
    assert.equal(typeof agent.act, "function", "act(world, day, tick)");
    assert.equal(agent.act.length, 3, "act takes (world, day, tick)");
    assert.ok(Array.isArray(agent.ledger), "analytics ledger is a plain array");
  }
  assert.equal(investor.holdDays, 180);
});

test("prototypes: the one-screen doctrine budget", () => {
  for (const [f, budget] of Object.entries(LINE_BUDGETS)) {
    const lines = readFileSync(join(protoDir, f), "utf8").split("\n").length;
    assert.ok(lines <= budget,
      `${f} is ${lines} lines (> ${budget}): plumbing is leaking ` +
      "into the agent -- move it into the world builder");
  }
});
