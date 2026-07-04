// The eqworld demo controller, driven with node-loaded dependencies:
// boot the equilibrium world, add a saver and a debtor DYNAMICALLY,
// tick a week of simulated days, and check the panels' data sources --
// status, roster, and the SVG charts.  (The exact shipped bundle is
// gated separately by eqdemo.bundle.test.js.)

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";

import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

import { tevmSession } from "../src/backends.js";
import { loadArtifact, artifactsAvailable, repoRoot } from "../src/nodefs.js";

let id = null;
try {
  id = await import("../src/identity.js");
} catch {
  // kernel not built
}
const urPath = (() => {
  try {
    return join(repoRoot(), "alberta_buck", "sim", "artifacts",
                "UniversalRouter.json");
  } catch { return null; }
})();
const skip = !id ? "identity kernel not built (make nix-core-build-wasm)"
  : !artifactsAvailable() ? "forge artifacts not built (make nix-build)"
  : !(urPath && existsSync(urPath)) ? "vendored UR artifact missing"
  : false;

test("eqapp: dynamic savers/debtors + live chart panels", { skip }, async () => {
  const { EqWorldApp } = await import("../demo/src/eqapp.js");

  const app = new EqWorldApp({
    session: await tevmSession(),
    identity: id.default,
    artifacts: loadArtifact,
    urArtifact: JSON.parse(readFileSync(urPath, "utf8")),
    makeAccount: () => privateKeyToAccount(generatePrivateKey()),
  });
  await app.boot();

  // No data before the first tick; the page shows live status anyway.
  assert.equal(app.charts(), null);
  const st0 = await app.status();
  assert.equal(st0.day, 0);
  assert.ok(st0.K > 0.5 && st0.K < 0.95);

  // The dynamic controls: one saver (short term so the cycle completes
  // inside the gate) and one debtor, added to the RUNNING world.
  const saverName = await app.addSaver({ holdDays: 4 });
  const debtorName = await app.addDebtor();
  assert.match(saverName, /Saver-1/);
  assert.equal(debtorName, "Debtor-1");

  for (let i = 0; i < 7; i++) await app.tick();

  const st = await app.status();
  assert.equal(st.day, 7);
  assert.equal(st.mismatches, 0, "every op matched");
  assert.ok(st.bvib > 0.7 && st.bvib < 1.3, `bvib sane (${st.bvib})`);
  assert.equal(app.world.series.length, 7, "one sample per tick");

  // Roster: the saver completed deposit -> term -> redeem; the debtor
  // booked its first month.
  const roster = app.roster();
  assert.equal(roster.length, 2);
  const saver = roster.find((r) => r.kind === "saver");
  const debtor = roster.find((r) => r.kind === "debtor");
  assert.equal(saver.state, "redeemed", JSON.stringify(saver));
  assert.ok(debtor.hypoNet <= 400_000, "counterfactual amortizing");
  assert.ok(debtor.net > 0, "net worth computed");

  // Charts: all five panels render SVG once there is data.
  const ch = app.charts();
  for (const key of ["k", "bvib", "price", "debtors", "savers"]) {
    assert.ok(ch[key]?.includes("<svg"), `${key} chart renders`);
  }
  assert.ok(ch.debtors.includes("hypo"), "counterfactual series labeled");

  // A second saver mid-run: the roster grows and the next tick engages it.
  await app.addSaver({ holdDays: 30 });
  await app.tick();
  assert.equal(app.roster().length, 3);
  assert.match(app.roster()[1].state, /holding receipt/);
});
