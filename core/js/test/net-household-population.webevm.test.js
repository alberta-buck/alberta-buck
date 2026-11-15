// The household population on the canonical (net) machine world: its opening cohorts by role and
// its arrivals drawn while the world runs, every member on the world's calendar. A net household's
// calendar day must equal the chain's (household-calendar.js), so the world's DayClock opens each
// day before the population acts, as it does first in eqsim's cast (Python: loop.py's warp).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import { loadAnyArtifact, artifactsAvailable, repoRoot, nodeWebevmSession } from "../src/nodefs.js";
import { HouseholdPopulationAgent } from "../src/agents/credit-household.js";
import { buildEquilibriumWorld, DayClock } from "../src/scenarios/eqworld.js";

let identity;
try { identity = (await import("../src/identity.js")).default; } catch { /* kernel unavailable */ }
const urPath = join(repoRoot(), "alberta_buck/sim/artifacts/UniversalRouter.json");
const skip = !identity || !artifactsAvailable() || !existsSync(urPath) ? "build kernels and contract artifacts first" : false;

test("a population adds its opening members and its arrivals on the machine world", { skip }, async () => {
  const session = await nodeWebevmSession();
  const w = await buildEquilibriumWorld(session, loadAnyArtifact, { identity, basket: "machine",
    urArtifact: JSON.parse(readFileSync(urPath, "utf8")) });
  const clock = new DayClock();
  const pop = new HouseholdPopulationAgent({ makeAccount: () => privateKeyToAccount(generatePrivateKey()),
    seed: 0xA1BC, days: 365, initial_debtors: 1, initial_savers: 1, initial_investors: 1,
    debtor_rate: 0, saver_rate: 0, investor_rate: 200, settings: { decision_days: 1 } });
  await pop.setup(w);
  for (let day = 0; day < 6; day++) {
    await clock.act(w, day, 0);
    await pop.act(w, day, 0);
  }
  const roles = pop.members.map((a) => a.p.role);
  assert.deepEqual(roles.slice(0, 3), ["debtor", "saver", "investor"]);
  assert.ok(roles.length > 3 && roles.slice(3).every((r) => r === "investor"), `roles ${roles}`);
  assert.equal(pop.members[3].member, "0/investor/1");
  assert.equal(pop.summary.investor.arrived, roles.length - 2);
  for (const a of pop.members) assert.equal(a.lastError, "", a.member);
  assert.equal(session.mismatches.length, 0);
});
