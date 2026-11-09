// Staged admission and its automatic dispatcher share one existing primary
// world. The same chronology runs in Python; no substitute cash/credit ledger.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { tevmSession } from "../src/backends.js";
import { buildEquilibriumWorld } from "../src/scenarios/eqworld.js";
import { CreditHouseholdAgent } from "../src/agents/credit-household.js";
import { TransactionReceiptPending } from "../src/session.js";
import { advanceTime, DAY } from "../src/buckworld.js";
import { artifact } from "../artifacts/bundle.mjs";
import * as identity from "../src/identity.js";

const RAW = 1_000_000n;
const spec = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-refinance.json", import.meta.url))).programme;
const BANK = "0x000000000000000000000000000000000000dEaD";

test("automatic debtors stage closing, resume cash and serialize calendar/receipt work", async (t) => {
  const s = await tevmSession();
  const world = await buildEquilibriumWorld(s, artifact, { identity, wheelArb: false, targetBuck: 1_000_000n * RAW,
    urArtifact: JSON.parse(readFileSync(new URL("../../../alberta_buck/sim/artifacts/UniversalRouter.json", import.meta.url))),
  });
  const actors = spec.members.map((member, i) => new CreditHouseholdAgent({
    account: { address: `0x${(0xabf000n + BigInt(i)).toString(16).padStart(40, "0")}` }, member, settings: spec.settings,
  }));
  const [a, b] = actors, ctr = {};
  for (const x of actors) { await x.setup(world); await world.households.openTrading(x.account); }
  assert.deepEqual(actors.map((x) => x.p.phase), spec.adoption_phases);
  world.householdEpoch = (await s.client.getBlock()).timestamp;
  const at = async (day) => {
    await advanceTime(world, world.householdEpoch + BigInt(day * DAY) - (await s.client.getBlock()).timestamp);
    await world.turnWheel(); await s.send(world.kctrl, "compute");
  };
  const step = (x, day, explicit = false) => x.advanceNetRefinancing(world, day, ctr,
    { forecasts: explicit ? spec.forecasts : {} });
  const principal = async (x) => (await world.netCreditOps.household(world.holderAddress(x.account), world._credits.get(x.account.address))).issued;
  for (const x of actors) {
    assert.equal(await step(x, 0), false);
    assert.ok(x.started); assert.equal(await s.call(world.usdc, "balanceOf", [world.holderAddress(x.account)]), BigInt(spec.seed_usdc));
    assert.equal(await principal(x), 0n); assert.equal(x.netRefinanceDay, null);
  }
  t.diagnostic("actual opening capital; different keyed adoption days");
  await at(1); await a.calendarMachine.prepare(world, 1);
  const before = { block: (await s.client.getBlock()).number, mortgage: a.mortgage, shadow: a.hypoMortgage };
  a.p.utilization = spec.insufficient_utilization;
  assert.equal((await a.assessNetRefinance(world, 1, spec.forecasts)).plan, null, "future K/capacity cannot finance the programme");
  a.p.utilization = spec.settings.utilization;
  const { plan, verdict, context } = await a.assessNetRefinance(world, 1, spec.forecasts);
  assert.equal((await s.client.getBlock()).number, before.block); assert.equal(a.mortgage, before.mortgage);
  assert.equal(a.hypoMortgage, before.shadow);
  assert.ok(plan, JSON.stringify(context, (_, v) => typeof v === "bigint" ? String(v) : v));
  assert.equal(context.reason, "worthwhile-staged-refinance");
  assert.ok(verdict.go && context.programme.score.go && context.programme.tranches > 1n);
  const fixed = spec.settings.switch_k * 1000, allocated = context.programme.fixed_cost_allocated;
  assert.equal(plan.switch_cost, BigInt(fixed) * RAW);
  assert.ok(allocated > 0 && allocated < fixed);
  assert.ok(verdict.value - fixed + allocated < 0, "the standalone first tranche would decline the full startup fee");
  assert.equal(-context.programme.score.parts.fixed, fixed);
  assert.ok(context.programme.economics.new_spendable > context.selected.economics.new_spendable);
  const concurrent = await Promise.allSettled([step(a, 1, true), step(a, 1, true)]);
  assert.equal(concurrent.filter((r) => r.status === "fulfilled" && r.value).length, 1);
  assert.equal(concurrent.filter((r) => r.status === "rejected" && /dispatcher is already/.test(r.reason)).length, 1);
  assert.equal(a.refis, 1, a.lastError); assert.ok(a.refinanceClosed); assert.equal(a.switchPaid, BigInt(fixed) * RAW);
  assert.equal(a.nextDecision, 2); assert.equal(await step(b, 1), false);
  let block = (await s.client.getBlock()).number, issued = await principal(a);
  assert.equal(await step(a, 1), false); assert.equal((await s.client.getBlock()).number, block);
  assert.equal(await principal(a), issued);
  t.diagnostic("programme admitted; one full closing fee and no duplicate same-day loan");

  await at(2);
  const pay = world.netSettlementOps.payExact;
  try {
    world.netSettlementOps.payExact = async (...args) => {
      const result = await pay(...args); throw new TransactionReceiptPending(result.receipt.transactionHash);
    };
    await step(a, 2);
  } finally { world.netSettlementOps.payExact = pay; }
  const m = a.refinanceMachine;
  assert.ok(m.active); assert.equal(m.flow.phase, "mortgage"); assert.equal(m.flow.status, "pending");
  issued = await principal(a);
  const paid = a.externalPaid, bank = await s.call(world.usdc, "balanceOf", [BANK]);
  await at(3); assert.ok(await step(a, 3));
  assert.equal(m.active, false); assert.equal(a.refis, 2); assert.equal(await principal(a), issued);
  assert.ok(a.externalPaid > paid); assert.equal(await s.call(world.usdc, "balanceOf", [BANK]), bank);
  assert.equal(a.switchPaid, BigInt(fixed) * RAW);

  await at(4);
  const start = a.startNetRefinance;
  try {
    a.startNetRefinance = (world, plan, ...args) =>
      start.call(a, world, { ...plan, minimum_usdc: plan.export_buck * 2n }, ...args);
    await step(a, 4);
  } finally { a.startNetRefinance = start; }
  assert.ok(m.settlementDue); assert.equal(m.flow.phase, "export"); assert.equal(a.refis, 2);
  issued = await principal(a);
  await at(5);
  const assess = a.assessNetRefinance;
  try {
    a.assessNetRefinance = () => { throw new Error("unsettled export cannot request new credit"); };
    assert.ok(await step(a, 5));
  } finally { a.assessNetRefinance = assess; }
  assert.equal(m.settlementDue, false); assert.equal(a.refis, 3, a.lastError); assert.equal(await principal(a), issued);
  t.diagnostic("lost bank receipt recovered once; next refused export resumed without a new loan");

  await at(6);
  issued = await principal(a); const retired = a.externalPaid, observe = world.netCreditOps.quoteGlobalReliefRate;
  try {
    world.netCreditOps.quoteGlobalReliefRate = () => { throw new Error("fund mark unavailable"); };
    assert.ok(await step(a, 6)); assert.equal(a.lastAction, "refinance-unavailable");
    assert.equal(await step(a, 6), false);
  } finally { world.netCreditOps.quoteGlobalReliefRate = observe; }
  assert.equal(await principal(a), issued); assert.equal(a.externalPaid, retired);
  assert.equal(m.events.at(-1).status, "unavailable"); assert.equal(a.nextDecision, 7);
  await at(7); assert.ok(await step(a, 7)); assert.equal(a.refis, 4, a.lastError);

  await at(9); b.p.utilization = spec.insufficient_utilization;
  assert.ok(await step(b, 9, true)); assert.equal(b.lastAction, "declined"); assert.equal(b.joined, false);
  assert.equal(b.nextDecision, 21); assert.equal(b.refinanceMachine.events.at(-1).status, "declined");
  await at(10); assert.equal(await step(b, 10), false); assert.equal(await principal(b), 0n);
  await at(21); b.p.utilization = spec.settings.utilization;
  assert.ok(await step(b, 21, true)); assert.equal(b.refis, 1, b.lastError); assert.equal(b.nextDecision, 22);
  await at(22); b.divestDay = 22;
  await assert.rejects(step(b, 22), /asset exits/);
  t.diagnostic("unavailable marks stop sends; pre-adoption declines retain cadence; issuers assess daily");

  await at(30); issued = await principal(a);
  const mint = a.calendarMachine.mint;
  try {
    a.calendarMachine.mint = async (...args) => {
      const receipt = await mint.apply(a.calendarMachine, args); throw new TransactionReceiptPending(receipt.transactionHash);
    };
    assert.ok(await step(a, 30));
  } finally { a.calendarMachine.mint = mint; }
  assert.ok(a.calendarMachine.active); assert.equal(a.incomeTotal, 0n); assert.equal(await principal(a), issued);
  assert.ok(await step(a, 30));
  assert.equal(a.calendarMachine.active, false); assert.equal(a.incomeTotal, a.incomeAnnual / 12n);
  assert.equal(await principal(a), issued); assert.equal(a.refis, 4);
  await at(31);
  try {
    world.netSettlementOps.payExact = async (...args) => { await pay(...args); throw new TransactionReceiptPending(null); };
    assert.ok(await step(a, 31));
  } finally { world.netSettlementOps.payExact = pay; }
  assert.ok(m.active); assert.equal(m.flow.status, "pending"); assert.equal(m.flow.hash, null);
  issued = await principal(a); const wages = a.incomeTotal, finalBank = await s.call(world.usdc, "balanceOf", [BANK]);
  await at(60); assert.ok(await step(a, 60));
  assert.equal(a.incomeTotal, wages); assert.equal(await principal(a), issued);
  assert.equal(await s.call(world.usdc, "balanceOf", [BANK]), finalBank);
  assert.equal(a.calendarMachine.incomeDue, a.incomeAnnual / 12n);
  assert.ok(a.hypoPaid > 0n && a.hypoInterest > 0n);
  assert.deepEqual(m.events.filter((e) => e.status === "planned" && e.kind !== "calendar").map((e) => e.day), [1, 2, 4, 7, 31]);
  assert.equal(a.netDispatching, false);
  t.diagnostic("wage recovery precedes new credit; unknown bank outcome blocks the next payday send");
});
