import test from "node:test";
import assert from "node:assert/strict";
import { runOptions } from "../dashboard/src/run-picker.js";

test("run choices sort stock fields numerically and display status and observed days", () => {
  const ids = [
    "stock-history-j2000000-i60-x0.1-n0-b-active",
    "stock-history-j2000000-i6-x0.1-n0-c-active",
    "stock-history-j0-i60-x0.1-n0-c-active",
    "stock-history-j2000000-i6-x0.05-n0-a-maintenance",
  ];
  const runs = ids.map((id, i) => ({
    id, status: i === 2 ? "failed" : "running", updated: new Date().toISOString(),
    frames: i + 1, scenario: { day_step: 2, days: 7 },
  }));
  const options = runOptions(runs);
  assert.deepEqual(options.map(([id]) => id), [ids[2], ids[3], ids[1], ids[0]]);
  assert.equal(options[0][1], `${ids[2]} (failed, 6 days)`);
  assert.equal(options[1][1], `${ids[3]} (running, 7 days)`);
  assert.deepEqual(runs.map((r) => r.id), ids);
});
