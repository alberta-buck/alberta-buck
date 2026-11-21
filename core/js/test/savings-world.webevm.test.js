// The savings sandbox's world in the tab (core/js/src/scenarios/savings-world.js), on webevm in Node:
// it boots the basket machine with economy.toml's cast, runs days, and each day's frame has what the
// savings page draws (sandbox/src/savings/model.js row()). The page's own wallet (Saver) then acts in
// it through the world's JSON-RPC, as it does against the Python server: enrolment, a TOKEN deposit,
// its receipt.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { loadAnyArtifact, artifactsAvailable, repoRoot, nodeWebevmSession } from "../src/nodefs.js";
import { SavingsWorld, SAVINGS_NODE_OPTIONS } from "../src/scenarios/savings-world.js";
import { row } from "../sandbox/src/savings/model.js";
import { Saver } from "../sandbox/src/savings/wallet.js";

let identity;
try { identity = (await import("../src/identity.js")).default; } catch { /* kernel unavailable */ }
const urPath = join(repoRoot(), "alberta_buck/sim/artifacts/UniversalRouter.json");
const skip = !identity || !artifactsAvailable() || !existsSync(urPath) ? "build kernels and contract artifacts first" : false;

test("the savings world runs days, frames them for the page, and serves the page's wallet", { skip }, async () => {
  const session = await nodeWebevmSession({ nodeOptions: SAVINGS_NODE_OPTIONS });
  // The economy's TOKENs and price paths (make sandbox-artifacts writes them).
  const pricesPath = join(repoRoot(), "core/js/artifacts/savings-prices.json");
  const prices = existsSync(pricesPath) ? JSON.parse(readFileSync(pricesPath, "utf8")) : null;
  const world = new SavingsWorld({ session, identity, artifacts: loadAnyArtifact, prices,
    urArtifact: JSON.parse(readFileSync(urPath, "utf8")),
    sets: { "agents.HouseholdPopulationAgent.initial_investors": "2", "scenario.seed": "7",
            "scenario.ticks_per_day": "4" } });
  let t = performance.now();
  await world.boot();
  const bootMs = performance.now() - t;
  assert.equal(world.population.initial_investors, 2, "the page's settings reach the population");
  assert.equal(world.ticksPerDay, 4, "ticks a day, from the page's settings");

  t = performance.now();
  const frames = [];
  const lowMarks = () => Promise.all(world.world.tokens.map(async (_, i) =>
    Number((await session.call(world.world.basket, "marks", [BigInt(i), 0n])).pLow)));
  frames.push(await world.step());
  const openingMarks = await lowMarks();
  const weights = await Promise.all(world.world.tokens.map(async (_, i) =>
    Number((await session.call(world.world.basket, "constituents", [BigInt(i)]))[9])));
  for (let d = 1; d < 3; d++) frames.push(await world.step());
  const closingMarks = await lowMarks();
  const weightsTotal = weights.reduce((a, b) => a + b, 0);
  const passive = closingMarks.reduce((sum, p, i) => sum + weights[i] * p / openingMarks[i], 0) / weightsTotal;
  assert.equal(row(frames[0]).hold, 1);
  assert.equal(row(frames.at(-1)).hold, passive, "fixed opening TOKEN quantities marked at today's LOW prices");
  const dayMs = (performance.now() - t) / 3;
  console.log(JSON.stringify({ bootMs: Math.round(bootMs), dayMs: Math.round(dayMs),
                               members: world.pop.members.length,
                               wheel: frames.map((f) => [f.wh_turns, f.wh_work, f.wh_settled]) }));
  const f = frames.at(-1);
  assert.equal(f.day, 2);
  // The clock: four ticks a day, each a quarter of a day in, the day still its own.
  const now = (await session.client.getBlock()).timestamp;
  const epoch = world.world.householdEpoch;
  assert.ok(now >= epoch + 2n * 86400n + 3n * 21600n && now < epoch + 3n * 86400n, "the last tick is late in day 2");
  const r = row(f);
  assert.ok(r.k > 0.5 && r.k < 1, `K ${r.k}`);
  assert.ok(r.bv > 0.9 && r.bv < 1.1, `BVIB ${r.bv}`);
  assert.ok(r.bu > 0.9 && r.bu < 1.1, `BUCK/USD ${r.bu}`);
  assert.equal(r.ref.length, world.world.tokens.length);
  if (prices) {
    assert.deepEqual(world.world.tokens.map((t) => t.sym), ["LABR", "CNST", "FOOD", "NRGC", "PAXG"],
      "the economy's five TOKENs");
    assert.deepEqual(r.ref, prices.series.history.map((s) => s[2]), "each pool pinned to its history");
  }
  assert.ok(r.su.every((p) => p > 0) && r.sb.every((p) => p > 0), "spot prices on every pool");
  assert.equal(f.sv?.kind, "equity", "the equity basket's savings block");
  assert.ok(r.sp > 0n || Number(f.sv.sp) > 0, "a share price");
  assert.ok(f.households && f.households.active >= 2, "the households' aggregate");
  assert.equal(r.weights.length, world.world.tokens.length, "held and target weights");
  // The wheel turned to completion: its pending() view read no work, or the settle hit its cap.
  assert.ok(f.wh_settled || f.wh_turns === world.keeper.maxTicks, `wheel ${f.wh_turns} ${f.wh_settled}`);

  // The page's wallet, through the world's JSON-RPC, exactly as through the Python server's.
  const link = {
    call: async (method, params = []) => {
      const [res] = await world.rpc([{ jsonrpc: "2.0", id: 1, method, params }]);
      if (res.error) throw Object.assign(new Error(res.error.message), res.error);
      return res.result;
    },
    batch: (reqs) => world.rpc(reqs.map((q, i) => ({ jsonrpc: "2.0", id: i + 1, ...q }))),
  };
  const info = await link.call("sim_info");
  assert.equal(info.basket_kind, "equity");
  assert.equal(info.tokens.length, world.world.tokens.length);
  const saver = new Saver({ link, info });
  await saver.enroll();
  assert.ok(await session.call(world.world.reg, "isVerified", [saver.address]), "enrolled as an identity");
  assert.equal(await session.call(world.world.buck, "balanceOf", [saver.address]), 0n);
  const tok = info.tokens[0];
  const amount = 10n ** BigInt(tok.decimals);           // one whole TOKEN, minted by the faucet
  const [mint, approve, deposit] = await saver.send([
    { to: tok.address, abi: (await import("../sandbox/src/savings/abi.js")).ERC20, functionName: "mint",
      args: [saver.address, amount] },
    { to: tok.address, abi: (await import("../sandbox/src/savings/abi.js")).ERC20, functionName: "approve",
      args: [info.basket, amount] },
    { to: info.basket, abi: (await import("../sandbox/src/savings/abi.js")).BASKET, functionName: "depositToken",
      args: [tok.address, amount, 0n] },
  ]);
  assert.ok(mint.ok && approve.ok, `${mint.reason} ${approve.reason}`);
  assert.ok(deposit.ok, `the deposit: ${deposit.reason}`);
  assert.equal(session.mismatches.length, 0);
});
