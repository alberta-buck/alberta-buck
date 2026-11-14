import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { ageOn, stage, drawdownShare, fundGrowth, YEAR_DAYS, profile, issuance, terms, refinance, refinanceQuoted, bestQuotedRefinance, surplusAction, investorAction, population, arrivalState, arrivalsStep, premium, saving,
  externalPremiumDue, stagedRefinance, ROLES } from "../src/agents/household-policy.js";
import { CreditHouseholdAgent } from "../src/agents/credit-household.js";
import { observeRefinanceForecasts } from "../src/agents/net-household.js";

const f = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-policy.json", import.meta.url)));
const insurance = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-insurance.json", import.meta.url)));
for (const c of f.closing) test(`household closing: ${c.name}`, () => {
  const p = profile(f.seed, "closing", { ...f.settings, switch_k: 2 });
  assert.equal(terms({ ...f.state, joined: c.joined, closing_paid: c.closing_paid }, p).fixed_cost, c.fixed);
});
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
test("one facility per asset and bounded portfolios preserve the other keyed thresholds", () => {
  for (const role of ROLES) assert.equal(profile(41404, "assets", { role }).asset_count, role === "investor" ? 0 : 1);
  const { asset_count: single, ...a } = profile(41404, "assets");
  const { asset_count: multiple, ...b } = profile(41404, "assets", { asset_count: 4 });
  assert.equal(single, 1); assert.equal(multiple, 4); assert.deepEqual(a, b);
  for (const count of [0, -1, 1.5, 33, true, NaN, "4"]) assert.throws(() => profile(41404, "assets", { asset_count: count }), /asset_count/);
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
    ["issuance_limit", NaN, /issuance limit/], ["expected_relief_rate", true, /relief/],
    ["premium_refund_fraction", true, /fraction/], ["expected_refund_growth", true, /growth/],
    ["expected_wallet_growth", true, /wallet/], ["activated_capacity", true, /activated credit/],
    ["issuance_limit", true, /issuance limit/], ["expected_relief_rate", "0.02", /relief/],
    ["expected_wallet_growth", Infinity, /wallet/]]) {
    assert.throws(() => terms({ ...f.state, ...f.net_refinance[0].state, [field]: value }, p), error);
  }
});
test("default forecasts keep absent age distinct and never hide a missing mark", async () => {
  for (const rate of [null, 0n, 20_000_000_000_000_000n]) {
    const observation = { blockNumber: 7n, totalSeconds: rate === null ? 0n : 100n * 31_557_600n,
      cashScaled: rate === null ? 0n : rate * 100n * 10n ** 36n / 10n ** 18n,
      positionMark: 0n, availableReliefScaled: 0n, annualRateWad: rate };
    const forecast = await observeRefinanceForecasts({ netCreditOps: { quoteGlobalReliefRate: async () => observation } });
    assert.equal(forecast.observation.annualRateWad, rate);
    assert.equal(forecast.source.annual_rate_wad, rate);
    assert.equal(forecast.values.expected_relief_rate, rate === null ? 0 : Number(rate) / 1e18);
    assert.equal(forecast.values.expected_wallet_growth, forecast.values.expected_refund_growth);
    assert.ok(forecast.values.expected_wallet_growth > -.02 && forecast.values.expected_wallet_growth < 0);
    assert.equal(forecast.basis, rate === null ? "no-claim-age/zero-relief/idle-cash" : "marked-fund/idle-cash");
  }
  await assert.rejects(observeRefinanceForecasts({ netCreditOps: {
    quoteGlobalReliefRate: async () => { throw new Error("guarded fund mark unavailable"); } } }), /guarded fund mark/);
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
const quotedTerms = (state = {}) => terms({ ...f.state, ...f.net_refinance.at(-1).state,
  spendable: 100000, expected_wallet_growth: .1, ...state }, profile(f.seed, "0", { ...f.settings, apr: .03, theta: 3 }));
for (const c of insurance.coverage) test(`external insurance: ${c.name}`, () => {
  const [premium, face, activated, due] = ["premium", "face", "activated", "due"].map((k) => BigInt(c[k]));
  assert.equal(externalPremiumDue(premium, face, activated), due);
  assert.equal(externalPremiumDue(premium, face, 0n), premium);
});
for (const c of insurance.refinance) test(`incremental insurance: ${c.name}`, () => {
  const t = { ...quotedTerms({ joined: c.joined }), fixed_cost: 2000 };
  const args = [t, 4900, c.new_spendable ? 0 : 5000, c.new_spendable, c.coverage, c.premium];
  const baseline = refinanceQuoted(...args), v = refinanceQuoted(...args, { externalPremiumSaving: c.saving });
  assert.equal(baseline.parts.premium, 0); assert.equal(v.parts.premium, c.saving);
  assert.ok(Math.abs(v.carry - baseline.carry - c.saving) < 1e-8);
  assert.ok(Math.abs(v.value - baseline.value - t.payback_y * c.saving) < 1e-8);
  assert.equal(v.parts.fixed, c.new_spendable ? -2000 : 0);
  assert.equal(refinance(t, 4900, 5000).parts.premium, 0);
});
test("external insurance refuses impossible coverage and unrelated savings", () => {
  for (const args of [[1n, 0n, 0n], [1n, 10n, 11n], [-1n, 10n, 0n], [1n, 10n, -1n],
    [true, 10n, 0n], [1n, 10.5, 0n], [1n << 256n, 10n, 0n]]) {
    assert.throws(() => externalPremiumDue(...args), /external insurance/);
  }
  for (const saving of [-1, NaN, true, 10001]) assert.throws(() =>
    refinanceQuoted(quotedTerms(), 4900, 0, 5000, 10000, 1000, { externalPremiumSaving: saving }), /incremental external/);
  assert.throws(() => refinanceQuoted(quotedTerms(), 4900, 5000, 0, 0, 0, { externalPremiumSaving: 1 }), /incremental external/);
  assert.throws(() => refinanceQuoted(quotedTerms(), 4900, 0, 5000, 0, 0, { externalPremiumSaving: 1 }), /incremental external/);
});
for (const c of f.quoted_refinance) test(`quoted refinance: ${c.name}`, () => {
  const t = quotedTerms(c.state), v = refinanceQuoted(t, c.usd, c.wallet, c.new_spendable, c.coverage, c.premium,
    { reserveCost: c.reserve_cost ?? 0, reserveCarry: c.reserve_carry ?? 0,
      premiumRefundFraction: c.premium_refund_fraction ?? null, expectedRefundGrowth: c.expected_refund_growth ?? null });
  assert.equal(v.go, c.go);
  assert.ok(Math.abs(v.parts.relief - (c.new_spendable + c.premium) * t.expected_relief_rate) < 1e-8);
  assert.ok(Math.abs(v.parts.wallet_carry + c.wallet * t.expected_wallet_growth) < 1e-8);
  assert.equal(v.parts.reserve_cost, -(c.reserve_cost ?? 0));
  assert.equal(v.parts.reserve_carry, -(c.reserve_carry ?? 0));
  const fraction = c.premium_refund_fraction ?? t.premium_refund_fraction;
  const growth = c.expected_refund_growth ?? t.expected_refund_growth;
  assert.ok(Math.abs(v.parts.deposit_write_down + c.premium * (1 - fraction)) < 1e-8);
  assert.ok(Math.abs(v.parts.deposit_age - c.premium * fraction * growth) < 1e-8);
});
for (const c of f.staged_refinance) test(`staged refinance: ${c.name}`, () => {
  const t = { ...quotedTerms(), fixed_cost: c.fixed, rate: .06 };
  const programme = { usd: 99000, wallet_used: 0, new_spendable: 100000, present_coverage: 150000, premium: 10000 };
  const tranche = Object.fromEntries(Object.entries(programme).map(([k, v]) => [k, v * (c.scale ?? .01)]));
  const { verdict, programme: whole } = stagedRefinance(t, tranche, programme);
  assert.equal(verdict.go, c.go);
  const standalone = bestQuotedRefinance(t, [tranche]);
  assert.equal(standalone.index !== null, c.standalone ?? false);
  if (verdict.go) {
    assert.equal(-verdict.parts.fixed, c.allocated);
    assert.ok(Math.abs(verdict.value * 100 - whole.value) < 1e-8);
    assert.equal(-whole.parts.fixed, c.fixed);
  }
  assert.throws(() => stagedRefinance({ ...t, issuance_limit: 5000 }, tranche, programme), /shared issuance limit/);
});
test("quoted refinance can retain a valuable wallet", () => {
  const t = quotedTerms(), old = refinance(t, 99000, 100000), retain = refinanceQuoted(t, 99000, 0, 100000, 0, 0);
  assert.equal(old.go, false);
  assert.ok(retain.go && retain.value > old.value);
});
const quotedCandidate = (c) => ({ usd: c.usd, wallet_used: c.wallet, new_spendable: c.new_spendable,
  present_coverage: c.coverage, premium: c.premium, reserve_cost: c.reserve_cost ?? 0, reserve_carry: c.reserve_carry ?? 0,
  premium_refund_fraction: c.premium_refund_fraction ?? null, expected_refund_growth: c.expected_refund_growth ?? null });
test("quoted refinance selects the best feasible candidate without unnecessary activation", () => {
  const choices = [0, 1, 2, 5].map((i) => quotedCandidate(f.quoted_refinance[i]));
  const selected = bestQuotedRefinance(quotedTerms(), choices);
  assert.equal(selected.index, 1);
  assert.ok(selected.verdict.go && selected.verdict.buck === 100000);
  assert.equal(bestQuotedRefinance(quotedTerms(), choices.slice(0, 1)).index, null);
  const t = quotedTerms({ expected_wallet_growth: -.02 });
  assert.equal(bestQuotedRefinance(t, [choices[1], choices[0]]).index, 1); // exact tie: no new debt
  assert.equal(bestQuotedRefinance(t, [{ ...choices[1], present_coverage: 500 }, choices[1]]).index, 1);
  const pooled = choices[2];
  assert.equal(bestQuotedRefinance(t, [{ ...pooled, premium_refund_fraction: .5 },
    { ...pooled, premium_refund_fraction: 1 }]).index, 1);
  assert.throws(() => bestQuotedRefinance(t, [{ ...choices[1], wallet_used: 100001 }]), /wallet funds/);
});
test("quoted refinance rejects inconsistent inputs", () => {
  const t = quotedTerms();
  assert.throws(() => refinanceQuoted(t, 400001, 0, 100000, 0, 0), /external debt/);
  assert.throws(() => refinanceQuoted(t, 99000, 100001, 100000, 0, 0), /wallet funds/);
  assert.throws(() => refinanceQuoted(t, 99000, 0, -1, 0, 0), /invalid/);
  assert.throws(() => refinanceQuoted(t, 99000, 0, 100000, 0, NaN), /invalid/);
  assert.throws(() => refinanceQuoted(t, 99000, "100", 100000, 0, 0), /invalid/);
  assert.throws(() => refinanceQuoted(t, 99000, 0, 100000, 0, 0, { reserveCost: true }), /invalid/);
  assert.throws(() => refinanceQuoted(t, 99000, 0, 0, 1, 0), /without issuance/);
  assert.throws(() => refinanceQuoted(t, 99000, 0, 100000, 0, 0, { reserveCost: 1 }), /without a premium/);
  assert.throws(() => refinanceQuoted(t, 99000, 0, 100000, 0, 1), /exceeds added coverage/);
  assert.throws(() => refinanceQuoted(t, 99000, 0, 100000, 0, 0, { premiumRefundFraction: 1.1 }), /refund fraction/);
  assert.throws(() => refinanceQuoted(t, 99000, 0, 100000, 0, 0, { expectedRefundGrowth: -1.1 }), /refund growth/);
  assert.throws(() => refinanceQuoted(t, 99000, 0, 100000, 0, 0, { premiumRefundFraction: NaN }), /refund forecast/);
  assert.throws(() => refinanceQuoted(t, 99000, 0, 100000000001, 0, 0), /shared issuance limit/);
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
test("net surplus guards delivered value and protects current credit capital", async () => {
  const { netSurplusPurchase, profile } = await import("../src/agents/household-policy.js");
  const { netPurchaseExpected } = await import("../src/net-market-quote.js");
  const { readFileSync } = await import("node:fs");
  const f = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-surplus.json", import.meta.url)));
  for (const c of f.cases) {
    const q = Object.fromEntries(Object.entries({ ...f.market, ...c.market }).map(([k, v]) => [k, BigInt(v)]));
    const args = Object.fromEntries(Object.entries({ ...f.amounts, ...c.amounts }).map(([k, v]) => [k, BigInt(v)]));
    const p = profile(41404, "surplus-vector", { ...f.settings, surplus_policy: c.policy });
    const r = netSurplusPurchase(q, args.cash, args.issued, args.fair_num, args.fair_den, args.cash_floor, p);
    assert.equal(r.action, c.action, c.name);
    if (c.spend !== undefined) assert.equal(r.spend_usdc, BigInt(c.spend), c.name);
    if (c.target !== undefined) assert.equal(r.target_principal, BigInt(c.target), c.name);
    if (c.spend_between) assert.ok(r.spend_usdc >= BigInt(c.spend_between[0]) && r.spend_usdc <= BigInt(c.spend_between[1]));
    assert.ok(r.spend_usdc <= (args.cash > args.cash_floor ? args.cash - args.cash_floor : 0n));
    assert.ok(r.target_principal >= 0n && r.target_principal <= args.issued);
    if (r.spend_usdc) {
      assert.equal(r.expected_net, netPurchaseExpected(q, r.spend_usdc));
      assert.ok(r.expected_net >= r.minimum_net && r.minimum_net > 0n);
      if (r.action === "retire" && c.policy !== "retire") {
        assert.ok(r.spend_usdc * args.fair_den * 100n < r.minimum_net * args.fair_num * 99n);
      }
    }
  }
});
test("net wallet takes one capped bite toward its money target", async () => {
  const { netWalletAdjustment } = await import("../src/agents/household-policy.js");
  const { netPurchaseExpected, netSaleExpected } = await import("../src/net-market-quote.js");
  const { readFileSync } = await import("node:fs");
  const f = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-wallet.json", import.meta.url)));
  let q, args;
  for (const c of f.cases) {
    q = Object.fromEntries(Object.entries({ ...f.market, ...c.market }).map(([k, v]) => [k, BigInt(v)]));
    args = Object.fromEntries(Object.entries({ ...f.amounts, ...c.amounts }).map(([k, v]) => [k, BigInt(v)]));
    const r = netWalletAdjustment(q, args.cash, args.held, args.target, args.cash_floor, f.settings);
    assert.deepEqual([r.action, r.reason], [c.action, c.reason], c.name);
    assert.equal(r.spend_usdc, BigInt(c.spend ?? 0), c.name); assert.equal(r.minimum_net, BigInt(c.minimum_net ?? 0), c.name);
    assert.equal(r.sell_buck, BigInt(c.sell ?? 0), c.name); assert.equal(r.minimum_usdc, BigInt(c.minimum_usdc ?? 0), c.name);
    assert.ok(r.spend_usdc <= (args.cash > args.cash_floor ? args.cash - args.cash_floor : 0n));
    assert.ok(r.spend_usdc <= q.reserve_usdc * 5000n / 1_000_000n && r.sell_buck <= q.reserve_buck * 5000n / 1_000_000n);
    assert.ok(!(r.spend_usdc && r.sell_buck));
    if (r.action === "buy") {
      assert.ok(args.held * 100n < args.target * 95n && r.minimum_net >= 1_000_000n && r.minimum_net <= r.expected_net);
      assert.equal(r.expected_net, netPurchaseExpected(q, r.spend_usdc));
    } else if (r.action === "sell") {
      assert.ok(args.held * 100n > args.target * 110n && r.sell_buck <= args.held - args.target);
      assert.ok(r.minimum_usdc >= 1_000_000n && r.minimum_usdc <= r.expected_usdc);
      assert.equal(r.expected_usdc, netSaleExpected(q, r.sell_buck));
    }
  }
  for (const bad of [{ cash: -1n }, { held: 1 }, { target: 1n << 256n }]) {
    const a = { ...args, ...bad };
    assert.throws(() => netWalletAdjustment(q, a.cash, a.held, a.target, a.cash_floor, f.settings), /invalid net wallet/);
  }
  // The target every runtime must size identically from one observation.
  const { netWalletTarget } = await import("../src/agents/household-policy.js");
  for (const c of f.targets.cases) {
    assert.equal(netWalletTarget(c.ratio === null ? null : BigInt(c.ratio), c.days, f.targets.annual_spending, f.targets.settings),
      BigInt(c.target), c.name);
  }
  assert.throws(() => netWalletTarget(1, 30, 60000, f.targets.settings), /ratio/);
});
test("each household keeps one keyed day of the month for its payday", async () => {
  const { firstPayday, profile } = await import("../src/agents/household-policy.js");
  const { readFileSync } = await import("node:fs");
  const f = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-calendar.json", import.meta.url))).paydays;
  for (const [arrive, payDay, first] of f.cases) {
    assert.equal(firstPayday(arrive, payDay), first);
    assert.ok(arrive < first && first <= arrive + 30 && first % 30 === payDay);
  }
  for (const bad of [[-1, 0], [0, 30], [0, -1], [0, 1.5], [1.5, 0]]) assert.throws(() => firstPayday(...bad), /pay day/);
  const days = Array.from({ length: 200 }, (_, i) => profile(41404, `payday/${i}`).pay_day);
  assert.ok(days.every((day) => Number.isSafeInteger(day) && day >= 0 && day < 30));
  assert.equal(new Set(days).size, 30, "a cohort's paydays spread over the whole month");
  assert.equal(profile(41404, "payday/0", { pay_day: 7 }).pay_day, 7);
  assert.deepEqual({ ...profile(41404, "payday/0", { pay_day: 7 }), pay_day: days[0] }, profile(41404, "payday/0"),
    "pinning the payday moves no other draw");
  for (const bad of [30, -1, NaN, "7"]) assert.throws(() => profile(41404, "payday/0", { pay_day: bad }), /pay_day/);
});

test("a fixed premium cost makes small loans proportionally dearer (as in Python)", () => {
  const base = { ...f.settings, premium_bp: [50, 50], premium_fixed_usd: 100 };
  const car = profile(f.seed, "0", { ...base, home_k: [20, 20] });
  const home = profile(f.seed, "0", { ...base, home_k: [500, 500] });
  assert.deepEqual([car.premium_bp, home.premium_bp], [100, 52]);
  assert.equal(profile(f.seed, "0", f.settings).premium_bp,
    profile(f.seed, "0", { ...f.settings, premium_fixed_usd: 0 }).premium_bp);
  assert.throws(() => profile(f.seed, "0", { ...f.settings, premium_fixed_usd: -1 }), /premium_fixed_usd/);
});

test("the lifecycle works, retires and draws down (as in Python)", () => {
  const p = profile(f.seed, "0", { ...f.settings, age: 40 });
  assert.deepEqual([p.retire_age, p.drawdown_years, p.usd_fund_share, p.usd_fund_return], [65, 25, 0, .08]);
  assert.equal(ageOn(p, 100, 100), 40);
  assert.ok(Math.abs(ageOn(p, 100, 100 + Math.trunc(10 * YEAR_DAYS)) - 50) < .01);
  assert.equal(stage(p, 100, 100), "working");
  assert.equal(drawdownShare(p, 100, 100), 0);
  const retired = 100 + Math.trunc(25 * YEAR_DAYS) + 1;
  assert.equal(stage(p, 100, retired), "retired");
  assert.ok(Math.abs(drawdownShare(p, 100, retired) / (30 / (25 * YEAR_DAYS)) - 1) < 1e-3);
  assert.equal(drawdownShare(p, 100, 100 + Math.trunc(50 * YEAR_DAYS) - 10), 1);
  assert.equal(stage(p, 100, 100 + Math.trunc(50 * YEAR_DAYS) + 1), "done");
  const off = profile(f.seed, "0", f.settings);
  assert.equal(ageOn(off, 0, 1000), null);
  assert.equal(stage(off, 0, 1000), null);
  assert.equal(drawdownShare(off, 0, 1000), 0);
  assert.ok(Math.abs(fundGrowth(.08, YEAR_DAYS) - 1.08) < 1e-12);
  for (const bad of [{ retire_age: 0 }, { drawdown_years: 0 }, { usd_fund_share: 1.5 }, { usd_fund_return: -1 }, { age: -1 }]) {
    assert.throws(() => profile(f.seed, "0", { ...f.settings, ...bad }));
  }
});
