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

test("ROI keeps first-close and explicit treatment baselines distinct, and missing data absent", async () => {
  const { headline, specs, merge, clipFrames } = await import("../dashboard/src/compare.js");
  const frames = [{day: 0, basket: {share_price: 1, hold_index: 1}}, {day: 1, basket: {share_price: 1.2, hold_index: 1.1}}, {day: 2, basket: {share_price: 1.3, hold_index: 1.2}}];
  const run = {id: "a", label: "a", frames, treatment_day: 1, baseline_share_price: 1.1};
  const h = headline(frames, run);
  assert.ok(Math.abs(h.share_roi - 30) < 1e-9);
  assert.ok(Math.abs(h.treatment_roi - 100 * (1.3 / 1.1 - 1)) < 1e-9);
  assert.equal(h.arrived, null); assert.equal(h.refused_days, null);
  const untreated = {...run, baseline_share_price: undefined};
  assert.equal(headline(frames, untreated).treatment_roi, null);
  const treatment = specs([run]).find((s) => s.id === 'treatment-roi').series[0];
  assert.equal(treatment.value(merge([run])[0]), null);
  assert.ok(Math.abs(treatment.value(merge([run])[2]) - h.treatment_roi) < 1e-9);
  assert.deepEqual(clipFrames(frames, 1).map((f) => f.day), [0,1]);
  assert.equal(frames.length, 3, 'clipping does not mutate cached frames');
});

test("share mark in USDC uses same-day bid and common available start", async () => {
  const { bidMark, commonBidBaseline, specs, merge } = await import("../dashboard/src/compare.js");
  const f = (day, share, bid) => ({day, basket: {share_price: share}, market: {book: {buck_bid: bid}}});
  const a = {id:'a',label:'active',frames:[f(0,1,null),f(1,1.1,2),f(2,1.2,2.1)]};
  const b = {id:'b',label:'maintenance',frames:[f(0,2,1),f(1,2,1.1),f(2,2.1,null)]};
  assert.equal(bidMark(a.frames[0]), null);
  assert.equal(commonBidBaseline([a,b]).day,1);
  const series=specs([a,b]).find(s=>s.id==='share-bid-roi').series, points=merge([a,b]);
  assert.equal(series[0].value(points[0]), null);
  assert.equal(series[1].value(points[2]), null);
  assert.ok(Math.abs(series[0].value(points[2]) - (2.52/2.2-1)*100) < 1e-9);
  const missing = {id:'c',label:'missing',frames:[f(0,1,null)]};
  assert.equal(commonBidBaseline([a,missing]).day,1, 'missing sibling does not hide available curve');
  assert.equal(specs([a,missing])[0].series[1].value(merge([a,missing])[0]), null);
});

test("through-day parsing, limits, and mismatched endpoints never fabricate deltas", async () => {
  const { parseThroughDay, effectiveEndDay, clipFrames } = await import("../dashboard/src/compare.js");
  assert.equal(parseThroughDay(null), null); assert.equal(parseThroughDay(""), null);
  assert.equal(parseThroughDay("365"), 365);
  for (const s of ["-1", "1.5", "1e2", "1x", "9007199254740992"]) assert.ok(Number.isNaN(parseThroughDay(s)));
  assert.equal(effectiveEndDay(729, 699), 699); assert.equal(effectiveEndDay(null, 699), 699);
  const base=[0,1,2].map((d)=>frame(d)), newer=[0,1,2,3].map((d)=>frame(d,.7));
  const unaligned=rows([{id:'a',label:'a',frames:base},{id:'b',label:'b',frames:newer}]);
  assert.equal(unaligned[1].comparable,false); assert.equal(unaligned[1].diff.k_end,null);
  const aligned=rows([{id:'a',label:'a',frames:base},{id:'b',label:'b',frames:clipFrames(newer,2)}]);
  assert.equal(aligned[1].comparable,true); assert.ok(aligned[1].diff.k_end<0);
  const late=rows([{id:'a',label:'a',frames:base},{id:'b',label:'b',frames:newer.slice(1,3)}]);
  assert.equal(late[1].diff.k_end,null);
});
