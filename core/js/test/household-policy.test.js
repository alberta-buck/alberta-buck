import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { profile, issuance, terms, refinance, surplusAction, investorAction, population, arrivalState, arrivalsStep, premium, saving,
  ROLES } from "../src/agents/household-policy.js";
import { CreditHouseholdAgent } from "../src/agents/credit-household.js";

const f = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-policy.json", import.meta.url)));
for (const c of f.issuance) test(`household policy: ${c.name}`, () => {
  const [kind, v] = issuance({ ...f.state, ...c.state }, profile(f.seed, "0", { ...f.settings, ...c.settings }));
  assert.equal(kind, c.kind); assert.equal(v.go, c.go);
  if (v.go) assert.ok(v.value > 0 && v.carry > 0);
});
for (const c of f.net_refinance) test(`net refinance: ${c.name}`, () => {
  const p = profile(f.seed, "0", { ...f.settings, apr: .03, theta: 3 });
  const t = terms({ ...f.state, ...c.state }, p), v = refinance(t, c.usd, c.buck);
  assert.equal(v.go, c.go);
  assert.ok(Math.abs(v.parts.relief - (Math.max(0, c.buck - t.headroom) + v.deposit) * t.expected_relief_rate) < 1e-8);
  assert.ok(Math.abs(v.parts.deposit_write_down + v.deposit * (1 - t.premium_refund_fraction)) < 1e-8);
  assert.ok(Math.abs(v.parts.deposit_age - v.deposit * t.premium_refund_fraction * t.expected_refund_growth) < 1e-8);
  assert.ok(Math.abs(v.parts.wallet_carry + Math.min(c.buck, t.headroom) * t.expected_wallet_growth) < 1e-8);
});
test("net refinance requires valid explicit forecasts", () => {
  const p = profile(f.seed, "0", f.settings);
  assert.throws(() => terms({ ...f.state, accounting: "net" }, p), /explicit/);
  assert.throws(() => terms({ ...f.state, ...f.net_refinance[0].state, premium_refund_fraction: 1.1 }, p), /refund fraction/);
  const missingCapacity = { ...f.state, ...f.net_refinance[0].state };
  delete missingCapacity.activated_capacity;
  assert.throws(() => terms(missingCapacity, p), /activated credit capacity/);
  const missingLimit = { ...f.state, ...f.net_refinance[0].state };
  delete missingLimit.issuance_limit;
  assert.throws(() => terms(missingLimit, p), /issuance limit/);
  for (const [field, value, error] of [["expected_relief_rate", -.01, /relief/],
    ["expected_refund_growth", -1.01, /growth/], ["expected_wallet_growth", -1.01, /wallet/],
    ["premium_refund_fraction", NaN, /fraction/], ["activated_capacity", -1, /activated credit/],
    ["activated_capacity", NaN, /activated credit/], ["issuance_limit", -1, /issuance limit/],
    ["issuance_limit", NaN, /issuance limit/]]) {
    assert.throws(() => terms({ ...f.state, ...f.net_refinance[0].state, [field]: value }, p), error);
  }
});
test("the shared issuance limit includes premium principal", () => {
  const p = profile(f.seed, "0", { ...f.settings, apr: .03, theta: 3 });
  const state = { ...f.state, ...f.net_refinance.at(-1).state,
    spendable: 20000, activated_capacity: 30000, unactivated: 1000000, issuance_limit: 40000 };
  const t = terms(state, p), e = t.premium_bp / 1000, spc = t.k * .99 - e;
  const max = 20000 + 30000 + 10000 * spc / (spc + e);
  assert.equal(refinance(t, 59000, 60000).go, false); // premium would exceed the common limit
  const [kind, decision] = issuance(state, p);
  assert.equal(kind, "refinance");
  assert.ok(decision.go && decision.buck <= max);
  const walletOnly = terms({ ...state, issuance_limit: 0 }, p);
  assert.equal(refinance(walletOnly, 19000, 20000).deposit, 0);
  assert.equal(refinance(walletOnly, 19000, 20001).go, false);
});
test("net activated capacity issues without another premium", () => {
  const p = profile(f.seed, "0", { ...f.settings, apr: .03, theta: 3 });
  const state = { ...f.state, ...f.net_refinance.at(-1).state }, t = terms(state, p);
  const v = refinance(t, 99000, 100000);
  assert.equal(v.go, true);
  assert.equal(v.coverage, 0);
  assert.equal(v.deposit, 0);
  assert.ok(Math.abs(v.parts.relief - 2000) < 1e-8);
  const [kind, decision] = issuance(state, p);
  assert.equal(kind, "refinance");
  assert.equal(decision.go, true);
  assert.ok(decision.buck > 0);
  assert.equal(decision.coverage, 0);
  assert.equal(decision.deposit, 0);
  const mixed = terms({ ...state, spendable: 20000, activated_capacity: 30000, unactivated: 1000000 }, p);
  const mixedV = refinance(mixed, 74000, 75000);
  assert.ok(Math.abs(mixedV.coverage - 25000 / (mixed.k * .99 - mixed.premium_bp / 1000)) < 1e-8);
  assert.ok(Math.abs(mixedV.parts.relief - (55000 + mixedV.deposit) * mixed.expected_relief_rate) < 1e-8);
  assert.ok(Math.abs(mixedV.parts.wallet_carry + 20000 * mixed.expected_wallet_growth) < 1e-8);
});
test("a maturity backlog blocks new credit without hiding wallet BUCK", async () => {
  const agent = new CreditHouseholdAgent({ account: "0x1", settings: { divest_rate: 0 } });
  const world = { accounting: "net", households: {
    read: async () => ({ drawn: 2_000_000n, spendable: 3_000_000n, unactivated: 10_000_000n,
      k: .75, ru: 100, rb: 100, fee: .0005, basket_usd: 1 }),
    activatedCapacity: async () => ({ capacity: 0n, sharedLimit: 10_000_000n,
      unactivatedPresent: 10_000_000n, maturitiesPending: true }),
  } };
  const state = await agent.state(world, 0);
  assert.equal(state.maturities_pending, true);
  assert.equal(state.activated_capacity, 0);
  assert.equal(state.issuance_limit, 0);
  assert.equal(state.unactivated, 0);
  assert.equal(state.spendable, 3);
});
test("household surplus policies use basket parity after trading costs", () => {
  for (const c of f.surplus) assert.equal(surplusAction(...c.slice(0, -1)), c.at(-1));
});
test("investors buy BUCKs unless they are dear", () => {
  for (const c of f.investor) assert.equal(investorAction(...c.slice(0, -1)), c.at(-1));
});
test("households save less as the BUCK grows dear, halving each step of premium", () => {
  for (const [spot, basket, fee, double, share] of f.saving) {
    assert.ok(Math.abs(saving(premium(spot, basket, fee), { paydown_double: double }) / share - 1) < 1e-6);
  }
});
const at = (series, day) => f.population[series].filter(([start]) => start <= day).at(-1)[1];
const arrivals = (gain, seed = f.seed) => {
  const p = population({ ...f.population.settings, gain }), state = arrivalState(seed, f.population.name);
  const days = Object.fromEntries(ROLES.map((r) => [r, []]));
  for (let day = 0; day < f.population.days; day++) {
    for (const [role] of arrivalsStep(state, seed, f.population.name, day, f.population.days,
                                      at("bvib", day), at("k", day), p)) days[role].push(day);
  }
  return days;
};
test("household arrivals reproduce, are keyed, and come sooner while the price gap draws them", () => {
  const base = arrivals(0), drawn = arrivals(50);
  assert.deepEqual(base, arrivals(0));
  assert.notDeepEqual(base, arrivals(0, f.seed + 1));
  const within = (days, lo, hi) => days.filter((d) => d >= lo && d < hi).length;
  for (const role of ["debtor", "saver"]) assert.ok(within(drawn[role], 130, 300) > within(base[role], 130, 300));
  assert.ok(within(drawn.investor, 330, 500) > within(base.investor, 330, 500));
  for (const role of ROLES) assert.ok(drawn[role].every((d, k) => k >= base[role].length || d <= base[role][k]));
  assert.throws(() => population({ max_members: 3 }), /max_members/);
  const ps = Array.from({ length: 40 }, (_, i) => profile(f.seed, `0/debtor/${i}`));
  assert.equal(new Set(ps.map(p => p.retire_disc)).size, 40);
  assert.ok(new Set(ps.map(p => p.phase)).size > 8);
});
