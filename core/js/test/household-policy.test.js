import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { profile, issuance, surplusAction, arrivals } from "../src/agents/household-policy.js";

const f = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-policy.json", import.meta.url)));
for (const c of f.issuance) test(`household policy: ${c.name}`, () => {
  const [kind, v] = issuance({ ...f.state, ...c.state }, profile(f.seed, "0", { ...f.settings, ...c.settings }));
  assert.equal(kind, c.kind); assert.equal(v.go, c.go);
  if (v.go) assert.ok(v.value > 0 && v.carry > 0);
});
test("household surplus policies use basket parity after trading costs", () => {
  for (const c of f.surplus) assert.equal(surplusAction(...c.slice(0, -1)), c.at(-1));
});
test("household arrivals continue, reproduce, and have an explicit size bound", () => {
  const schedule = arrivals(f.seed, 0, 1827);
  assert.deepEqual(schedule, arrivals(f.seed, 0, 1827));
  assert.ok(schedule.at(-1) > 1500);
  assert.notDeepEqual(schedule, arrivals(f.seed + 1, 0, 1827));
  assert.throws(() => arrivals(f.seed, 0, 1827, 10, 100, 12), /max_members/);
  const ps = Array.from({ length: 40 }, (_, i) => profile(f.seed, `0/${i}`));
  assert.equal(new Set(ps.map(p => p.retire_disc)).size, 40);
  assert.ok(new Set(ps.map(p => p.phase)).size > 8);
});
