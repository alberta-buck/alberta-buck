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
  assert.deepEqual(chartData(spec("price"), frames).series.map((s) => s.values), [[0.99, 1.01, 1.02], [1.01, 0.995, 0.99]]);
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
