// Node test of the viewer's model pipeline against real vectors (no browser):
//   node alberta_buck/sim/viewer/test_viewer.mjs VECTOR.json [VECTOR2.json ...]
// Prints the model summary and asserts the invariants the page relies on.
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const require = createRequire(import.meta.url);
const V = require("./viewer.js");

let failures = 0;
for (const path of process.argv.slice(2)) {
  const vec = JSON.parse(readFileSync(path, "utf8"));
  const m = V.buildModel(vec);
  const cls = Object.fromEntries(m.classes.map(c => [c.cls, c]));
  const withActs = Object.keys(m.agents.acts).length;
  const nActs = Object.values(m.agents.acts).reduce((s, l) => s + l.length, 0);
  console.log(`== ${path}`);
  console.log(`   frames ${m.frames.length} days ${m.days[0]}..${m.days[m.days.length - 1]}  telemetry v${m.version}  roster ${m.roster.length}  series ${Object.keys(m.series).length}`);
  console.log(`   canonical panes: ${m.canonical.map(x => x[0].split(" ")[0]).join(", ")}`);
  console.log(`   groups: ${Object.entries(m.groups).map(([g, k]) => g + ":" + k.length).join(" ")}`);
  console.log(`   classes: ${m.classes.map(c => `${c.cls} x${c.count} (${c.withState} state, ${c.agents.length - c.withState} named, ${c.blanks} blank, ${c.aggKeys.length} agg)`).join("; ")}`);
  console.log(`   agents with acts: ${withActs}, acts total ${nActs}`);
  try {
    assert.ok(m.frames.length > 0, "frames");
    assert.ok(m.canonical.some(x => x[1].includes("basketVal")), "bvib pane");
    assert.ok(m.canonical.some(x => x[1].includes("buckK")), "K pane");
    assert.ok(m.classes.length > 0, "classes");
    // every scenario population appears as a class, blanks = count - roster
    const pop = ((m.meta.experiment || {}).scenario || {}).agents || {};
    for (const [c, n] of Object.entries(pop)) {
      assert.ok(cls[c], `class tab for ${c}`);
      assert.equal(cls[c].count, Number(n), `count of ${c}`);
      assert.equal(cls[c].blanks, Math.max(0, Number(n) - cls[c].agents.length), `blanks of ${c}`);
    }
    // per-agent pens exist for every roster agent that has a frame record
    for (const a of m.roster) {
      const pens = m.agents.pens[a.id];
      if (pens) assert.ok(Object.values(pens).some(arr => arr.some(v => v != null)), `pens of ${a.id}`);
    }
    // nearest acts: sorted, at most 3, closest to the day
    if (nActs) {
      const id = Object.keys(m.agents.acts)[0];
      const list = m.agents.acts[id];
      const mid = m.days[Math.floor(m.days.length / 2)];
      const near = V.nearestActs(list, mid, 3);
      assert.ok(near.length <= 3 && near.length > 0, "nearest acts");
      for (let i = 1; i < near.length; i++) assert.ok(near[i].day >= near[i - 1].day, "acts ordered");
      const first = list.find(a => a.kind);
      console.log(`   sample act (${id}): ${JSON.stringify(first || list[0]).slice(0, 200)}`);
      console.log(`   nearest to day ${mid}: ${near.map(a => `d${a.day}t${a.t} ${a.kind || a.fn}${a.ok === false ? " FAILED" : ""}`).join(" | ")}`);
    }
    if (m.version >= 2) assert.ok(m.roster.every(a => "telemetry" in a), "v2 roster flags");
    console.log("   OK");
  } catch (e) { failures++; console.log("   FAIL " + e.message); }
}
process.exit(failures ? 1 : 0);
