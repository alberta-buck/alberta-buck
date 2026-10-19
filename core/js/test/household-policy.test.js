import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { profile, issuance, surplusAction, investorAction, population, arrivalState, arrivalsStep, premium, saving,
  ROLES } from "../src/agents/household-policy.js";

const f = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-policy.json", import.meta.url)));
for (const c of f.issuance) test(`household policy: ${c.name}`, () => {
  const [kind, v] = issuance({ ...f.state, ...c.state }, profile(f.seed, "0", { ...f.settings, ...c.settings }));
  assert.equal(kind, c.kind); assert.equal(v.go, c.go);
  if (v.go) assert.ok(v.value > 0 && v.carry > 0);
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
