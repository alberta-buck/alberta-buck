// The actual primary builder and household; finite policy vectors are shared
// separately with Python. No second economy or simulated monetary ledger.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { tevmSession } from "../src/backends.js";
import { buildEquilibriumWorld } from "../src/scenarios/eqworld.js";
import { CreditHouseholdAgent } from "../src/agents/credit-household.js";
import { artifact } from "../artifacts/bundle.mjs";
import * as identity from "../src/identity.js";

const RAW = 1_000_000n;
const spec = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-refinance.json", import.meta.url))).programme;

test("staged household pays the full closing once and issues only its bounded current leg", async (t) => {
  const s = await tevmSession();
  const world = await buildEquilibriumWorld(s, artifact, { identity, wheelArb: false, targetBuck: 1_000_000n * RAW,
    urArtifact: JSON.parse(readFileSync(new URL("../../../alberta_buck/sim/artifacts/UniversalRouter.json", import.meta.url))),
  });
  t.diagnostic("primary world ready");
  const account = { address: `0x${0xabf000n.toString(16).padStart(40, "0")}` };
  const a = new CreditHouseholdAgent({ account, member: "staged-refinance", settings: spec.settings });
  await a.setup(world); await world.households.openTrading(account);
  const owner = world.holderAddress(account);
  await s.send(world.usdc, "mint", [owner, BigInt(spec.seed_usdc)]);
  const block = (await s.client.getBlock()).number, mortgage = a.mortgage, supply = await s.call(world.buck, "totalIssued");
  a.p.utilization = spec.insufficient_utilization;
  assert.equal((await a.assessNetRefinance(world, 1, spec.forecasts)).plan, null, "future K/capacity cannot finance the programme");
  a.p.utilization = spec.settings.utilization;
  const { plan, verdict, context } = await a.assessNetRefinance(world, 1, spec.forecasts);
  assert.equal((await s.client.getBlock()).number, block); assert.equal(a.mortgage, mortgage);
  assert.ok(plan, JSON.stringify(context, (_, v) => typeof v === "bigint" ? String(v) : v));
  assert.equal(context.reason, "worthwhile-staged-refinance");
  assert.ok(verdict.go && context.programme.score.go && context.programme.tranches > 1n);
  const fixed = spec.settings.switch_k * 1000;
  assert.equal(plan.switch_cost, BigInt(fixed) * RAW);
  const allocated = context.programme.fixed_cost_allocated;
  assert.ok(allocated > 0 && allocated < fixed);
  assert.ok(verdict.value - fixed + allocated < 0, "the standalone first tranche would decline the full startup fee");
  assert.equal(-context.programme.score.parts.fixed, fixed);
  assert.ok(context.programme.economics.new_spendable > context.selected.economics.new_spendable);
  t.diagnostic("programme admitted from current credit and capital");
  await a.startNetRefinance(world, plan, 1, {}, { context });
  assert.equal(a.refinanceMachine.status().status, "complete", a.lastError);
  assert.equal(a.refinanceClosed, true); assert.equal(a.switchPaid, BigInt(fixed) * RAW);
  assert.equal(a.mortgage, mortgage - plan.mortgage_limit); assert.equal(a.hypoMortgage, mortgage);
  assert.equal(await s.call(world.buck, "totalIssued"), supply + plan.candidate.requestedSpendable + plan.candidate.premium);
  const second = await a.assessNetRefinance(world, 1, spec.forecasts);
  assert.ok(second.plan); assert.equal(second.plan.switch_cost, 0n); assert.equal(Math.abs(second.verdict.parts.fixed), 0);
  await a.startNetRefinance(world, second.plan, 1, {}, { context: second.context });
  assert.equal(a.refinanceMachine.status().status, "complete", a.lastError);
  assert.equal(a.refis, 2); assert.equal(a.switchPaid, BigInt(fixed) * RAW);
  t.diagnostic("two bank allocations confirmed with one closing fee");
  assert.equal(a.externalPaid, plan.mortgage_limit + second.plan.mortgage_limit);
  assert.equal(await s.call(world.usdc, "balanceOf", ["0x000000000000000000000000000000000000dEaD"]),
    a.externalPaid + a.switchPaid + a.penaltyPaid);
  const third = await a.assessNetRefinance(world, 1, spec.forecasts);
  const bad = { ...third.plan, minimum_usdc: third.plan.export_buck * 2n };
  await a.startNetRefinance(world, bad, 1, {}, { context: third.context });
  assert.equal(a.refinanceMachine.status().phase, "export");
  assert.ok(a.refinanceMachine.settlementDue && !a.refinanceMachine.paymentDue);
  const retained = await s.call(world.buck, "totalIssued");
  await assert.rejects(a.assessNetRefinance(world, 1, spec.forecasts), /idle canonical/);
  await assert.rejects(a.startNetRefinance(world, third.plan, 1), /unsettled export/);
  assert.equal(await s.call(world.buck, "totalIssued"), retained);
  assert.equal(a.refis, 2); assert.equal(a.switchPaid, BigInt(fixed) * RAW);
});
