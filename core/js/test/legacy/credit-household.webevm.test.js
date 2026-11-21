// Exercise the portable household's real credit, lender cash and savings custody on both baskets.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import { decodeFunctionData, encodeFunctionData } from "viem";
import { loadAnyArtifact, artifactsAvailable, repoRoot, nodeWebevmSession } from "../../src/nodefs.js";
import { CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent }
  from "../../src/agents/credit-household.js";
import { issuance } from "../../src/agents/household-policy.js";
import { buildEquilibriumWorld } from "../../src/scenarios/eqworld.js";
import { advanceTime, DAY } from "../../src/buckworld.js";

let identity;
try { identity = (await import("../../src/identity.js")).default; } catch { /* kernel unavailable */ }
const urPath = join(repoRoot(), "alberta_buck/sim/artifacts/UniversalRouter.json");
const skip = !identity || !artifactsAvailable() || !existsSync(urPath) ? "build kernels and contract artifacts first" : false;
const E6 = 1_000_000n;
const settings = { home_k: 1000, ltv: .2, apr: .055, premium_bp: 50, theta: 3, cash_k: 100,
  buffer_k: 10, switch_k: 0, risk: 0, decision_days: 1, income_frac: .1, living_frac: .5, divest_rate: 0,
  money_months: 0 };

test("machine bootstrap distributes its capital by declared token weights", { skip }, async () => {
  const s = await nodeWebevmSession();
  const tokens = [{ sym: "A", name: "A", dec: 18, p0: 2_500_000n, weightBp: 7500 },
    { sym: "B", name: "B", dec: 18, p0: 4_000_000n, weightBp: 2500 }];
  const w = await buildEquilibriumWorld(s, loadAnyArtifact, { identity, basket: "machine", tokens,
    urArtifact: JSON.parse(readFileSync(urPath, "utf8")) });
  const declared = await Promise.all(tokens.map((_, i) => s.call(w.basket, "constituents", [BigInt(i)])));
  assert.deepEqual(declared.map((c) => Number(c[9])), [7500, 2500]);
  const seeded = w.tokens.map((t) => t.seedTok * t.p0 / (10n ** BigInt(t.dec)));
  assert.ok(seeded[0] >= 3n * seeded[1] - 1n && seeded[0] <= 3n * seeded[1] + 1n);
  const actual = await s.call(w.basket, "responseWeightsBp");
  assert.ok(Math.abs(Number(actual[0]) - 7500) < 300, `opening weight ${actual[0]} bp`);
  assert.equal(s.mismatches.length, 0);
});

// TODO(S3): port to the world's calendar (a DayClock each day, and every payday of the year acted
// rather than one 365-day jump: the net calendar refuses to skip one). It pins what
// net-household-exit does not: a partial redemption keeping its claim, a release refused after K
// moves, a failed release verification, a broke household's shortfall.
test("household exit retains partial claims, retries release and never sells credit", { skip }, async () => {
  const s = await nodeWebevmSession();
  const w = await buildEquilibriumWorld(s, loadAnyArtifact, { identity, basket: "machine",
    urArtifact: JSON.parse(readFileSync(urPath, "utf8")) });
  const a = new CreditSaverAgent({ account: privateKeyToAccount(generatePrivateKey()), member: "exit-retry",
    settings: { ...settings, carry_utilization: .3, max_impact_bp: 2000, surplus_policy: "basket" } });
  await a.setup(w); await a.act(w, 0, 0);
  assert.ok(a.receipts.length && (await w.households.read(a.account)).drawn > 0n);
  const open = a.ledger.at(-1);
  assert.equal(open.basketAtRisk, a.basketPrincipal);
  assert.equal(open.basketMarkAvailable, true);
  assert.equal(open.basketMark, a.shares * await s.call(w.basket, "sharePrice") / (10n ** 18n));
  assert.equal(open.basketRoi, Number(open.basketMark - open.basketAtRisk) / Number(open.basketAtRisk));
  await advanceTime(w, 365 * DAY);                       // include an aged insurance refund
  const send = s.send.bind(s), call = s.call.bind(s);
  let partial = true, unavailable = true, changedK = false, refusals = 0;
  let verifyPending = false, verificationFailures = 0;
  s.call = async (contract, name, args = []) => {
    if (verifyPending && contract.address === w.credit.address && name === "creditInfo") {
      verifyPending = false; verificationFailures += 1;
      throw new Error("verification unavailable");
    }
    return call(contract, name, args);
  };
  s.send = async (contract, name, args = [], opts = {}) => {
    if (partial && opts.tag === "household:redeem") {
      const decoded = decodeFunctionData({ abi: w.basket.abi, data: args[1] });
      args = [args[0], encodeFunctionData({ abi: w.basket.abi, functionName: "redeem",
        args: [decoded.args[0], 5000n, 0n] })];
    }
    if (opts.tag === "household:release") {
      if (unavailable) throw new Error("release unavailable");
      if (!changedK) {
        changedK = true;
        await send(w.kctrl, "setRails", [0n, await w.K() * 99n / 100n]);
        const r = await send(contract, name, args, opts);
        assert.equal(r.status, "reverted");
        assert.match(s.lastRevertReason, /insufficient coverage to unwind/);
        refusals += 1;
        return r;
      }
    }
    const result = await send(contract, name, args, opts);
    if (opts.tag === "household:release" && result.status === "success") verifyPending = true;
    return result;
  };
  a.divestDay = 1;
  const principal = a.basketPrincipal;
  await a.act(w, 1, 0);
  assert.ok(a.receipts.length && a.basketPrincipal > 0n && a.basketPrincipal < principal);
  assert.equal(a.ledger.at(-1).basketAtRisk, a.basketPrincipal, "partial exit removes only the settled basis");
  assert.equal(a.exited, false, "partial redemption retains the claim");
  partial = false;
  let day = 2;
  for (; day < 30 && !a.lastError; day++) await a.act(w, day, 0);
  assert.match(a.lastError, /release-coverage:.*release unavailable/);
  assert.ok(!a.released && !a.exited);
  for (let i = 0; i < 3; i++, day++) {
    await a.act(w, day, 0);
    assert.ok((await w.households.read(a.account)).signed >= 0n, "no new lien while waiting to release");
  }
  unavailable = false;
  for (; day < 80 && !a.exited; day++) {
    await a.act(w, day, 0);
    if (a.lastError.includes("verification unavailable")) assert.ok(!a.released && !a.exited);
    else assert.equal(a.lastError, "", `day ${day}`);
  }
  assert.ok(a.exited && a.released && a.exitStage === "complete");
  assert.equal(refusals, 1); assert.equal(a.exitRetries, 1);
  assert.equal(verificationFailures, 1, "release is confirmed only after every state read succeeds");
  const final = await w.households.read(a.account);
  assert.ok(final.signed >= 0n && final.limit === 0n && final.deposit === 0n);
  assert.equal(a.receipts.length, 0);
  assert.equal(a.ledger.at(-1).basketAtRisk, 0n);
  assert.equal(a.ledger.at(-1).basketRoi, null, "a closed position has no open-position return");
  const cash = await w.households.cash(a.account);
  await a.act(w, 100, 0);
  assert.equal(await w.households.cash(a.account), cash);

  const broke = new CreditHouseholdAgent({ account: privateKeyToAccount(generatePrivateKey()), member: "exit-short",
    settings: { ...settings, cash_k: 0 } });
  await broke.setup(w); await broke.act(w, 0, 0);
  broke.sold = true; broke.divestDay = 1;       // sale proceeds were exhausted by other obligations
  await broke.act(w, 1, 0);
  assert.ok(broke.exitShort && !broke.exited && !broke.released && broke.mortgage > 0n);
  assert.equal(s.mismatches.length, 0);
});
