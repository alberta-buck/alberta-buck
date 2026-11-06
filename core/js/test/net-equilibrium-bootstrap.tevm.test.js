// The existing equilibrium builder, with actual linked Buck and the same
// basket/fund graph as the wallet stack. Household lifecycle comes next.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { encodeFunctionData, keccak256, toBytes } from "viem";
import { tevmSession } from "../src/backends.js";
import { buildEquilibriumWorld } from "../src/scenarios/eqworld.js";
import { artifact } from "../artifacts/bundle.mjs";
import { worldRecord } from "../src/buckworld.js";
import { createNetMath } from "../src/net-math.js";
import * as identity from "../src/identity.js";

test("canonical equilibrium bootstrap reuses one fund/basket and issues actual pool money", async () => {
  const s = await tevmSession();
  const world = await buildEquilibriumWorld(s, artifact, { identity,
    targetBuck: 100_000n * 1_000_000n,
    urArtifact: JSON.parse(readFileSync(new URL("../../../alberta_buck/sim/artifacts/UniversalRouter.json", import.meta.url))),
    tokens: [{ sym: "CNST", name: "Construction", dec: 18, p0: 2_500_000n, weightBp: 7500 },
      { sym: "FOOD", name: "Food", dec: 18, p0: 4_000_000n, weightBp: 2500 }],
  });
  const { buck, credit, basket, net, wheel } = world;
  assert.equal(world.accounting, "net");
  assert.equal(await s.call(buck, "ACCOUNTING_MODEL"), keccak256(toBytes("Buck/compounded-cash/per-facility-jubilee/v1")));
  assert.equal(basket.address, net.basket.address);
  assert.equal(world.v3f.address, net.factory.address);
  assert.equal((await s.call(net.fund, "basket")).toLowerCase(), basket.address.toLowerCase());
  assert.equal((await s.call(buck, "jubilee")).toLowerCase(), net.fund.address.toLowerCase());
  assert.equal((await s.call(net.fund, "valuationWheel")).toLowerCase(), wheel.address.toLowerCase());
  assert.equal((await s.call(net.fund, "investmentKeeper")).toLowerCase(), wheel.address.toLowerCase());
  assert.equal(await s.call(wheel, "triangleCount"), 0n, "canonical arb still needs fee/inventory qualification");
  assert.equal(await s.call(world.reg, "isCarrying", [wheel.address]), true);
  assert.equal(await s.call(world.reg, "carryingFrozen", [wheel.address]), true);
  assert.notEqual(await s.call(buck, "receiptFragment", [s.account.address, wheel.address]), `0x${"00".repeat(32)}`);
  const actual = await s.call(basket, "responseWeightsBp");
  assert.ok(Math.abs(Number(actual[0]) - 7500) < 300, `opening weight ${actual[0]} bp`);
  let issued = 0n;
  const count = await s.call(credit, "totalSupply");
  for (let id = 0n; id < count; id++) issued += (await s.call(credit, "issuanceOf", [id]))[0];
  assert.equal(await s.call(buck, "totalIssued"), issued);
  assert.ok(issued > 0n);
  assert.ok((await s.call(credit, "issuanceOf", [await s.call(basket, "creditId")]))[0] > 0n);
  for (let i = 0; i < world.tokens.length; i++) {
    assert.ok(await s.call(basket, "liquidityOf", [BigInt(i)]) > 0n);
    assert.ok(await s.call(buck, "balanceOf", [world.tokens[i].poolBuck.address]) > 0n);
  }
  // The existing proxy helper now opts into the actual production insurer.
  // Seed a funding reserve from already-issued bootstrap surplus, then
  // exercise the shared selected-facility operation with a real premium.
  const account = s.account;
  await world.pledgeInsured(account, [100_000_000n], 100n, { tag: "bootstrap:household" });
  const owner = world.holderAddress(account), [id] = world._credits.get(account.address);
  assert.equal((await s.call(credit, "insurerOf", [id])).toLowerCase(), world.operator.account.address.toLowerCase());
  await s.send(world.simlp, "exec", [buck.address, encodeFunctionData({ abi: buck.abi,
    functionName: "transfer", args: [owner, 20_000_000n] })]);
  const before = await world.netCreditOps.household(owner, [id]);
  const supply = await s.call(buck, "totalIssued");
  const [, beforeNet] = await s.call(buck, "accountBalancesScaled", [owner]);
  const beforeTime = (await s.client.getBlock()).timestamp;
  const issuedTx = await world.netCreditOps.issue(owner, id, 100_000_000n, 40_000_000n, 10_000_000n, world.poolAcct);
  const after = await world.netCreditOps.household(owner, [id]);
  const issueTime = (await s.client.getBlock({ blockNumber: issuedTx.blockNumber })).timestamp;
  const math = createNetMath(createRequire(import.meta.url)("alberta-buck-kernel/math"));
  const expectedNet = math.decay(beforeNet, issueTime - beforeTime) + 40_000_000n * math.scale;
  assert.equal(after.issued, 50_000_000n, "cash plus full-value premium are actual principal");
  assert.equal(after.premiumNominal, 10_000_000n);
  assert.equal((await s.call(buck, "accountBalancesScaled", [owner]))[1], expectedNet);
  assert.equal(after.spendable, expectedNet / math.scale, "the existing reserve decays before fresh issuance arrives");
  assert.equal(before.issued, 0n);
  assert.equal(await s.call(buck, "totalIssued"), supply + 50_000_000n);
  assert.equal((await s.call(buck, "facilityPool", [id])).toLowerCase(), world.poolAcct.toLowerCase());
  assert.equal(s.mismatches.length, 0);
  assert.throws(() => worldRecord(world), /equilibrium save\/restore/);
});
