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
import { createInsurerPool, worldRecord } from "../src/buckworld.js";
import { createNetMath } from "../src/net-math.js";
import * as identity from "../src/identity.js";
import { CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent } from "../src/agents/credit-household.js";
import { advanceTime, DAY } from "../src/buckworld.js";

test("canonical equilibrium bootstrap reuses one fund/basket and issues actual pool money", async (t) => {
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
  await t.test("existing household roles set up separate facilities and insurer-specific marks", async () => {
    const Q = 10n ** 36n, RAW = 1_000_000n;
    const agents = [];
    for (const [i, Agent] of [CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent].entries()) {
      const account = { address: `0x${(0xabc00n + BigInt(i)).toString(16).padStart(40, "0")}` };
      const a = new Agent({ account, member: `books-${i}`, settings: { home_k: 1000, premium_bp: 100 } });
      await a.setup(world);
      agents.push(a);
      const owner = world.holderAddress(account), ids = world._credits.get(account.address) ?? [];
      assert.equal(await s.call(world.reg, "isVerified", [owner]), true);
      assert.equal(await s.call(world.reg, "isCarrying", [owner]), false);
      assert.equal(ids.length, a.investor ? 0 : 4);
      for (const tid of ids) {
        assert.equal((await s.call(credit, "ownerOf", [tid])).toLowerCase(), owner.toLowerCase());
        assert.equal((await s.call(credit, "insurerOf", [tid])).toLowerCase(), world.operator.account.address.toLowerCase());
        assert.equal((await s.call(credit, "creditInfo", [tid]))[1], 0n);
        assert.deepEqual(await s.call(credit, "issuanceOf", [tid]), [0n, 0n]);
      }
      const mark = await world.netCreditOps.markHousehold(owner, ids);
      assert.equal(mark.state.issued + mark.state.spendable + mark.premiumNetScaled + mark.reliefScaled, 0n);
      assert.equal(mark.creditLimit, 0n);
      await assert.rejects(a.act(world, 0, 0), /decisions await/);
      assert.equal(a.started, false);
      assert.equal(a.hypoCash, 0n);
    }
    const [debtor, saver, investor] = agents;
    const owner = world.holderAddress(debtor.account), proxy = await world.proxyFor(debtor.account);
    const ids = world._credits.get(debtor.account.address), [first, second] = ids;
    const execute = (p, c, name, args) => s.send(p, "exec", [c.address,
      encodeFunctionData({ abi: c.abi, functionName: name, args })]);
    await execute(world.simlp, buck, "transfer", [owner, 20n * RAW]);
    await world.netCreditOps.issue(owner, first, 100n * RAW, 40n * RAW, 10n * RAW, world.poolAcct);
    for (const [receiver, amount] of [[saver, 5n * RAW], [investor, 7n * RAW]]) {
      const address = world.holderAddress(receiver.account);
      await execute(proxy, buck, "transfer", [address, amount]);
      const mark = await world.netCreditOps.markHousehold(address, world._credits.get(receiver.account.address) ?? []);
      assert.equal(mark.state.spendable, amount);
      assert.equal(mark.state.issued, 0n);
    }
    assert.equal((await world.netCreditOps.markHousehold(owner, ids)).state.issued, 50n * RAW);
    await advanceTime(world, 365 * DAY + 6 * 3600);
    const newPool = await createInsurerPool(world, world.operator);
    await world.netCreditOps.issue(owner, second, 100n * RAW, 40n * RAW, 10n * RAW, newPool.address);
    const mark = await world.netCreditOps.markHousehold(owner, ids);
    assert.equal(mark.state.issued, 100n * RAW);
    assert.equal(mark.state.premiumNominal, 20n * RAW);
    assert.deepEqual(mark.state.facilities.slice(0, 2).map((f) => f.insurerPool.toLowerCase()),
      [world.poolAcct.toLowerCase(), newPool.address.toLowerCase()]);
    const values = await Promise.all([world.poolAcct, newPool.address].map((p) => s.call(buck, "accountBalancesScaled", [p])));
    assert.equal(mark.premiumNetScaled, values.reduce((sum, [gross, net]) => sum + 10n * RAW * Q * net / gross, 0n));
    assert.ok(mark.premiumNetScaled < 20n * RAW * Q);
    assert.ok(values[0][1] < values[0][0] && values[1][1] === values[1][0]);
    const relief = (await Promise.all([first, second].map((tid) => s.call(net.fund, "previewRelief", [tid, 50n * RAW]))))
      .reduce((sum, quote) => sum + quote[0], 0n);
    assert.equal(mark.reliefScaled, relief);
    assert.ok(relief > 0n);
    assert.equal(mark.exceedsLiquidityLimit, relief > mark.availableReliefScaled);
    await assert.rejects(world.netCreditOps.markHousehold(world.holderAddress(saver.account), ids), /foreign/);
    await assert.rejects(world.netCreditOps.markHousehold(owner, [first, first]), /duplicate/);
    const deadline = (await s.client.getBlock()).timestamp + 3600n;
    await world.netCreditOps.authorizeReduction(owner, first, RAW, 0n, deadline);
    assert.equal(await world.households.pending(debtor.account), true);
    await debtor.act(world, 0, 0);
    assert.equal(debtor.lastAction, "repay-pending");
    assert.equal(debtor.started, false);
    await world.netCreditOps.cancelReduction(owner, first);
  });
  assert.equal(s.mismatches.length, 0);
  assert.throws(() => worldRecord(world), /equilibrium save\/restore/);
});
