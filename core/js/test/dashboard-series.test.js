// The dashboard's data layer on the shared run-record fixture (test/fixtures/run-record.json,
// whose frames alberta_buck/test/test_runrecord.py proves are what the Python recorder writes).
// No DOM and no EVM: what any source's frames turn into on the Overview.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { OVERVIEW, chartData, format, runStatus, stackTops, tiles, ticks, windowFrames } from "../dashboard/src/series.js";

const FIXTURE = JSON.parse(readFileSync(new URL("../../../test/fixtures/run-record.json", import.meta.url)));
const frames = FIXTURE.frames;
const spec = (id) => OVERVIEW.find((s) => s.id === id);

test("every Overview chart reads the recorded frame shape", () => {
  for (const s of OVERVIEW) {
    const d = chartData(s, frames);
    assert.deepEqual(d.days, [0, 1, 2]);
    for (const series of d.series) {
      assert.ok(series.values.every((v) => v === null || Number.isFinite(v)), `${s.id}.${series.key}`);
      assert.ok(series.values.some((v) => v !== null), `${s.id}.${series.key} is recorded in the fixture`);
    }
  }
  assert.deepEqual(chartData(spec("price"), frames).series.map((s) => s.values),
    [[0.99, 1.01, 1.02], [null, 1.015, 1.0275], [1.01, 0.995, 0.99]], "BVIB, quoted BVIB (none while the pools are fresh), BUCK/USD");
  assert.deepEqual(chartData(spec("population"), frames).series.map((s) => s.values.at(-1)), [2, 1, 3]);
  assert.deepEqual(chartData(spec("health"), frames).series.map((s) => s.values), [[0, 1, 2], [0, 0, 1]]);
});

test("stacks sum to the whole: holders to supply, sources to supply", () => {
  for (const id of ["holders", "sources"]) {
    const tops = stackTops(chartData(spec(id), frames));
    assert.deepEqual(tops.at(-1).map((v) => Math.round(v * 1e6)), frames.map((f) => Number(f.supply.total)), id);
  }
  const gaps = stackTops({ days: [0, 1], series: [{ values: [1, null] }, { values: [2, null] }] });
  assert.deepEqual(gaps, [[1, null], [3, null]], "a day with no value is left out, not drawn as zero");
});

test("windows, tiles, status, formats and ticks", () => {
  assert.deepEqual(windowFrames(frames, "all").map((f) => f.day), [0, 1, 2]);
  assert.deepEqual(windowFrames(frames, "2").map((f) => f.day), [1, 2]);
  const t = Object.fromEntries(tiles(frames, { scenario: { days: 365 } }).map((x) => [x.id, x]));
  assert.equal(t.day.value, 2); assert.equal(t.day.detail, "of 365");
  assert.equal(t.supply.value, 42.5); assert.equal(t.households.value, 6); assert.equal(t.households.detail, "1 exited");
  assert.equal(t.refused.value, 2); assert.equal(t.faults.value, 1);
  assert.deepEqual(tiles([], {}), []);
  const now = Date.parse("2026-11-14T12:00:00Z");
  assert.equal(runStatus({ status: "running", updated: "2026-11-14T11:59:30Z" }, now), "running");
  assert.equal(runStatus({ status: "running", updated: "2026-11-14T11:50:00Z" }, now), "stale");
  assert.equal(runStatus({ status: "complete" }, now), "complete");
  assert.equal(format(null, "ratio"), "n/a"); assert.equal(format(1.02044, "ratio"), "1.0204");
  assert.equal(format(31_927_606, "buck"), "31.93M"); assert.equal(format(950, "buck"), "950.00");
  assert.equal(format(1234.4, "count"), "1,234");
  assert.deepEqual(ticks(0.98, 1.03, 5), [0.98, 0.99, 1, 1.01, 1.02, 1.03]);
  assert.deepEqual(ticks(0, 32e6, 4), [0, 1e7, 2e7, 3e7]);
});

test("the BuckBasket, Market and Jubilee tabs read the recorded frame shape", async () => {
  const { BASKET, MARKET, JUBILEE, tabTiles } = await import("../dashboard/src/series.js");
  const tokens = FIXTURE.tokens;
  for (const specs of [BASKET(tokens), MARKET(tokens), JUBILEE()]) {
    for (const s of specs) {
      const d = chartData(s, frames);
      for (const series of d.series) {
        assert.ok(series.values.every((v) => v === null || Number.isFinite(v)), `${s.id}.${series.key}`);
        assert.ok(series.values.some((v) => v !== null), `${s.id}.${series.key} is recorded in the fixture`);
      }
    }
  }
  const lev = chartData(BASKET(tokens).find((s) => s.id === "leverage"), frames).series;
  assert.ok(lev[0].values[2] === 0 && Math.abs(lev[1].values[2] - 11.2 / 16.2) < 1e-12 && lev[2].values[2] === 0.747,
    "the fixture's basket holds all it owes: K bounds nothing");
  const limit = chartData(BASKET(tokens).find((s) => s.id === "limit"), frames).series;
  assert.deepEqual([limit[0].values[2], limit[1].values[2]], [11.2, 10.95], "lien + the contract's headroom (under water)");
  const { basketLimit } = await import("../dashboard/src/series.js");
  const old = { control: { k: 0.5 }, basket: { lien: 4e6, equity: 6e6, held_buck: 3e6 } };
  assert.equal(basketLimit(old), 6e6, "without the contract's headroom: K x equity + held BUCK (the net mark)");
  assert.deepEqual(chartData(BASKET(tokens).find((s) => s.id === "wheel"), frames).series[0].values, [null, 1, 1],
    "a cumulative counter is drawn as its daily change");
  assert.deepEqual(chartData(BASKET(tokens).find((s) => s.id === "weights"), frames).series.map((s) => s.values[2]), [-2, 2]);
  assert.deepEqual(chartData(MARKET(tokens).find((s) => s.id === "carry"), frames).series.map((s) => s.values[2]),
    [0.99, 0.995, 0.998], "each pool's BUCK net of its carried fee");
  // The price book: the BUCK's bid and ask beside its spot, its fair dollar value, and the routes.
  const usd = chartData(MARKET(tokens).find((s) => s.id === "buck_usd"), frames).series;
  assert.deepEqual(usd.map((s) => s.values[2]), [frames[2].market.buck_usd, 1.01, 1.01202, 1.03]);
  assert.equal(usd[1].values[0], null, "no book before it samples");
  assert.deepEqual(chartData(MARKET(tokens).find((s) => s.id === "routes"), frames).series[0].values.slice(0, 3), [null, null, 1]);
  const gap = chartData(MARKET(tokens).find((s) => s.id === "triangle"), frames).series[0].values[2];
  assert.ok(Math.abs(gap - (0.249 * 0.99 / 0.251 - 1) * 100) < 1e-9);
  const pools = stackTops(chartData(MARKET(tokens).find((s) => s.id === "pools"), frames));
  assert.equal(Math.round(pools.at(-1)[2] * 1e6), 14_800_000 + 8_900_000 + 6_700_000);
  const older = frames.map((f) => ({ ...f, market: { ...f.market, pools: null } }));
  const together = chartData(MARKET(tokens, false).find((s) => s.id === "pools"), older);
  assert.deepEqual(together.series.map((x) => x.key), ["dollar", "tokens"], "a run without per-pool BUCK shows the pools' total");
  assert.equal(Math.round(stackTops(together).at(-1)[2] * 1e6), 14_800_000 + 15_600_000);
  const t = Object.fromEntries(tabTiles("basket", frames).map((x) => [x.id, x.value]));
  assert.ok(Math.abs(t.le - 11.2 / 16.2) < 1e-12 && t.equity === 16.2 && t.headroom === -0.25);
  assert.deepEqual(tabTiles("nothing", frames), []);
});

test("the Agents table's columns and an event's detail", async () => {
  const { COLUMNS, eventDetail } = await import("../dashboard/src/agents.js");
  const row = { member: "0/debtor/1", role: "debtor", day: 9, nw: 2_000_000, hypo: 5_000_000, drawn: 7_000_000, exited: false, error: "tx reverted" };
  const v = Object.fromEntries(COLUMNS.map((c) => [c.key, c.value(row)]));
  assert.equal(v.vs, -3); assert.equal(v.drawn, 7); assert.equal(v.basket, null); assert.equal(v.state, "refused: tx reverted");
  assert.equal(format(-3, "usd"), "-$3.00");
  assert.equal(v.stage, "") ; assert.equal(v.age, null);
  const life = Object.fromEntries(COLUMNS.map((c) => [c.key, c.value({ ...row, age: 66.7, stage: "retired" })]));
  assert.equal(life.stage, "retired"); assert.equal(life.age, 66);
  assert.equal(eventDetail({ seq: 4, day: 2, kind: "surplus", phase: "x", status: "confirmed", accounts: { big: 1 }, operation: "deposit", amounts: { buck: "5" } }),
    '{"operation":"deposit","amounts":{"buck":"5"}}');
});

test("the life-stage chart reads the population's stages, and is empty without them", () => {
  const stages = OVERVIEW.find((x) => x.id === "stages");
  const with_ = [{ day: 0, population: { roles: {}, stages: { young: 4, middle: 16, retired: 6 } } }];
  assert.deepEqual(stages.series.map((x) => x.value(with_[0])), [4, 16, 6]);
  assert.deepEqual(stages.series.map((x) => x.value({ day: 0, population: { roles: {}, stages: null } })), [null, null, null]);
});
