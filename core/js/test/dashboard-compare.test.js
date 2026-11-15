// The dashboard's Compare tab, on the data side: which runs a comparison's selection draws, the
// settings two runs differ in, the day they first part, and the differences the table shows. No DOM
// and no EVM (core/js/dashboard/src/compare.js; the runner is alberta_buck/sim/compare.py).
import { test } from "node:test";
import assert from "node:assert/strict";

import { departures, divergence, flatten, headline, merge, rows, selection, settingsDiff, signed, specs } from "../dashboard/src/compare.js";

const index = {
  name: "leak", axes: [
    { name: "leak", reference: "year", values: [{ label: "year", set: {} }, { label: "none", set: { "deploy.leak_days": 0 } },
      { label: "three-years", set: { "deploy.leak_days": 1095 } }] },
    { name: "prices", reference: "history", values: [{ label: "history", set: {} }, { label: "revert", set: {} }] },
  ],
  runs: [],
};
for (const leak of ["year", "none", "three-years"]) for (const prices of ["history", "revert"]) {
  index.runs.push({ id: `leak.leak-${leak}.prices-${prices}`, values: { leak, prices } });
}

const frame = (day, k = 0.75, extra = {}) => ({ day, control: { k, bvib: 1 + day / 1000 }, market: { buck_usd: 1 },
  supply: { total: 24e12, basket_lien: 6e12, household_principal: 0 },
  population: { roles: { investor: { arrived: 30, active: 30, exited: 0 } } }, health: { errors: 0, faults: 0 }, ...extra });

test("a selection varies one axis and holds the others, reference first", () => {
  assert.deepEqual(selection(index, "leak").map((r) => r.id),
    ["leak.leak-year.prices-history", "leak.leak-none.prices-history", "leak.leak-three-years.prices-history"]);
  assert.deepEqual(selection(index, "leak", { prices: "revert" }).map((r) => r.label), ["year", "none", "three-years"]);
  assert.deepEqual(selection(index, "prices", { leak: "none" }).map((r) => r.id),
    ["leak.leak-none.prices-history", "leak.leak-none.prices-revert"]);
  assert.deepEqual(selection(index, "nothing"), []);
});

test("the settings two runs differ in come from their manifests", () => {
  const base = { scenario: { seed: 7 }, experiment: { source: "/a/economy.toml", deploy: { k0: 0.75, leak_days: 365 },
    scenario: { seed: 7, prices: "history" } } };
  const other = { scenario: { seed: 7 }, experiment: { source: "/b/economy.toml", deploy: { k0: 0.75, leak_days: 0 },
    scenario: { seed: 7, prices: "history" } } };
  assert.deepEqual(settingsDiff(other, base), [{ key: "deploy.leak_days", a: "0", b: "365" }]);
  assert.deepEqual(settingsDiff(base, base), [], "where it was read from is not a setting");
  assert.equal(flatten({ a: { b: [1, 2] } })["a.b"], "[1,2]");
});

test("divergence: the first day two runs part, on the watched observables and anywhere", () => {
  const a = [0, 1, 2, 3].map((d) => frame(d));
  assert.deepEqual(divergence(a, a.map((f) => ({ ...f }))), { watched: null, any: null, days: 4 });
  const b = [0, 1, 2, 3].map((d) => frame(d, d >= 2 ? 0.74 : 0.75, d >= 1 ? { basket: { x: 1 } } : {}));
  assert.deepEqual(divergence(b, a), { watched: 2, any: 1, days: 3 }, "compared until both are found");
});

test("the table's figures, differences from the reference, and the charts' merged frames", () => {
  const runs = [{ id: "r0", label: "year", frames: [0, 1, 2].map((d) => frame(d)) },
    { id: "r1", label: "none", frames: [0, 1, 2].map((d) => frame(d, 0.75 - d / 100)) }];
  const [ref, run] = rows(runs);
  assert.equal(ref.diverges, null);
  assert.equal(run.diverges.watched, 1);
  assert.ok(Math.abs(run.diff.k_end + 0.02) < 1e-12 && run.diff.supply_end === 0);
  assert.equal(headline([]).days, 0);
  const merged = merge(runs);
  assert.deepEqual(merged.map((f) => Object.keys(f.runs)), [["year", "none"], ["year", "none"], ["year", "none"]]);
  const k = specs(runs).find((s) => s.id === "k");
  assert.deepEqual(k.series.map((s) => s.value(merged[2])), [0.75, 0.73]);
  assert.equal(signed(-0.000012, "ratio"), "-0.000012");
  assert.equal(signed(0, "count"), "±0");
  const third = { id: "r2", label: "late", frames: [0, 1, 2].map((d) => frame(d, d === 2 ? 0.7 : 0.75)) };
  assert.deepEqual(departures(rows([...runs, { ...runs[1], id: "r3", label: "again" }, third])),
    [{ day: 1, label: "none, again depart" }, { day: 2, label: "late departs" }], "one marker a day");
});
