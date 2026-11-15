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
  const world = new SavingsWorld({ session, identity, artifacts: loadAnyArtifact,
    urArtifact: JSON.parse(readFileSync(urPath, "utf8")),
    sets: { "agents.HouseholdPopulationAgent.initial_investors": "2", "scenario.seed": "7" } });
  let t = performance.now();
  await world.boot();
  const bootMs = performance.now() - t;
  assert.equal(world.population.initial_investors, 2, "the page's settings reach the population");

  t = performance.now();
  const frames = [];
  for (let d = 0; d < 3; d++) frames.push(await world.step());
  const dayMs = (performance.now() - t) / 3;
  console.log(JSON.stringify({ bootMs: Math.round(bootMs), dayMs: Math.round(dayMs),
                               members: world.pop.members.length }));
  const f = frames.at(-1);
  assert.equal(f.day, 2);
  const r = row(f);
  assert.ok(r.k > 0.5 && r.k < 1, `K ${r.k}`);
  assert.ok(r.bv > 0.9 && r.bv < 1.1, `BVIB ${r.bv}`);
  assert.ok(r.bu > 0.9 && r.bu < 1.1, `BUCK/USD ${r.bu}`);
  assert.equal(r.ref.length, world.world.tokens.length);
  assert.ok(r.su.every((p) => p > 0) && r.sb.every((p) => p > 0), "spot prices on every pool");
  assert.equal(f.sv?.kind, "equity", "the equity basket's savings block");
  assert.ok(r.sp > 0n || Number(f.sv.sp) > 0, "a share price");
  assert.ok(f.households && f.households.active >= 2, "the households' aggregate");
  assert.equal(r.weights.length, world.world.tokens.length, "held and target weights");

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
