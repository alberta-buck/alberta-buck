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
import { buyBuck, sellBuck, netPurchaseFromReceipt, netSaleFromReceipt, tradingOpen } from "../src/market.js";
import { TransactionReceiptPending, TransactionReverted } from "../src/session.js";

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
  let family;
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
    family = agents;
  });
  await t.test("normal keeper work publishes the fund's guarded LP mark", async () => {
    for (let day = 0; day < 3; day++) {
      await advanceTime(world, DAY);
      await world.turnWheel({ tag: "keeper:mark" });
    }
    assert.ok(await s.call(net.fund, "positionId") > 0n);
    const publication = await s.call(wheel, "publishedValuation");
    assert.equal(publication[5], true);
    assert.equal(publication[1], await s.call(net.fund, "reliefPositionMark"));
    assert.ok((await world.netCreditOps.markHousehold(basket.address,
      [await s.call(basket, "creditId")])).reliefScaled > 0n);
  });
  await t.test("primary proxy market authenticates net buys, bounded sales and separate bank payments", async () => {
    const RAW = 1_000_000n, debtor = family[0], account = debtor.account;
    const owner = world.holderAddress(account), holder = { address: owner };
    const ids = world._credits.get(account.address);
    await s.send(world.simlp, "exec", [buck.address, encodeFunctionData({ abi: buck.abi,
      functionName: "transfer", args: [owner, 200n * RAW] })]);
    await world.households.openTrading(account);
    assert.equal(await tradingOpen(world, world.netMarket, owner), true, "public proxies use BUCK's identity fallback");
    const candidate = await world.netCreditOps.assessIssueCandidate(owner, ids[2], 1000n * RAW, 500n * RAW, 100n * RAW);
    assert.deepEqual(candidate.reasons, []);
    const issued = await world.netCreditOps.issueCandidate(owner, candidate);
    assert.equal(issued.principal, 600n * RAW);
    assert.equal(issued.premium, 100n * RAW);
    const initialMortgage = debtor.mortgage, initialPaid = debtor.externalPaid;
    const supply = await s.call(buck, "totalIssued"), before = await world.households.read(account);
    const sold = await sellBuck(world, world.netMarket, holder, 100n * RAW, { minOut: 90n * RAW });
    assert.equal(sold.nominalPaid, 100n * RAW);
    assert.equal(await world.households.cash(account) - before.cash, sold.received);
    assert.equal((await world.netCreditOps.household(owner, ids)).issued, before.drawn);
    assert.equal(await s.call(buck, "totalIssued"), supply);
    assert.equal(debtor.mortgage, initialMortgage);
    assert.equal(debtor.externalPaid, initialPaid, "sale proceeds are not an external-debt payment");
    const receiver = await world.proxyFor({ address: `market-recipient:${account.address}` });
    const bought = await buyBuck(world, world.netMarket, holder, 10n * RAW,
      { minOut: 9n * RAW, recipient: receiver.address });
    assert.ok(bought.nominalReceived > bought.received && bought.received >= 9n * RAW);
    assert.equal(await s.call(buck, "netBalanceScaled", [receiver.address]), bought.receivedScaled);
    const boughtReceipt = await s.client.getTransactionReceipt({ hash: bought.transactionHash });
    assert.throws(() => netPurchaseFromReceipt(world, world.netMarket, holder, boughtReceipt, bought.transactionHash),
      /missing or ambiguous/);
    const guarded = await world.households.buy(account, RAW, { minNet: RAW / 2n });
    assert.ok(guarded.spent > 0n && guarded.got >= RAW / 2n);
    for (const c of [world.router, world.swapGuard]) assert.equal(await s.call(buck, "balanceOf", [c.address]), 0n);
    assert.equal(await s.call(world.usdc, "balanceOf", [world.swapGuard.address]), 0n);
    for (const c of [world.permit2, world.swapGuard]) assert.equal(await s.call(world.reg, "isVerified", [c.address]), false);
    const cash = await world.households.cash(account);
    await assert.rejects(buyBuck(world, world.netMarket, holder, RAW, { minOut: 2n * RAW }), TransactionReverted);
    assert.equal(await world.households.cash(account), cash);
    await assert.rejects(sellBuck(world, world.netMarket, holder, RAW, { minOut: 0n }), /positive input and minimum output/);
    const dispatch = world.netMarket.execute;
    let lost;
    world.netMarket.execute = async (...args) => {
      const receipt = await dispatch(...args);
      throw new TransactionReceiptPending(receipt.transactionHash);
    };
    try { await sellBuck(world, world.netMarket, holder, RAW, { minOut: 1n, recipient: receiver.address }); }
    catch (err) { assert.ok(err instanceof TransactionReceiptPending); lost = err; }
    finally { world.netMarket.execute = dispatch; }
    assert.ok(lost?.transactionHash);
    const receipt = await s.client.getTransactionReceipt({ hash: lost.transactionHash });
    const recovered = netSaleFromReceipt(world, world.netMarket, holder, receipt,
      { expectedHash: lost.transactionHash, expectedInput: RAW, minimumOut: 1n, recipient: receiver.address });
    assert.equal(recovered.received, await s.call(world.usdc, "balanceOf", [receiver.address]));
    assert.throws(() => netSaleFromReceipt(world, world.netMarket, holder, receipt,
      { expectedHash: lost.transactionHash, expectedInput: RAW, minimumOut: 1n }), /missing or ambiguous/);
    const bank = "0x000000000000000000000000000000000000dEaD";
    assert.equal((await world.netSettlementOps.payExact(owner, bank, 20n * RAW)).amount, 20n * RAW);
    assert.equal(await s.call(world.usdc, "balanceOf", [bank]), 20n * RAW);
    assert.equal(debtor.mortgage, initialMortgage);
    assert.equal(debtor.externalPaid, initialPaid);
    assert.equal(await s.call(buck, "totalIssued"), supply);
    assert.equal((await debtor.state(world, 370)).drawn, Number(before.drawn) / 1e6);
  });
  await t.test("personal records use actual portfolios and exact integer money", async () => {
    const Q = 10n ** 36n, RAW = 1_000_000n;
    const [debtor, saver, investor] = family;
    const owner = world.holderAddress(debtor.account), proxy = await world.proxyFor(debtor.account);
    const execute = (p, c, name, args) => s.send(p, "exec", [c.address,
      encodeFunctionData({ abi: c.abi, functionName: name, args })]);
    const transfer = (p, to, amount) => execute(p, buck, "transfer", [to, amount]);
    await transfer(proxy, world.simlp.address, 50n * RAW);
    debtor.savings = await world.proxyFor({ address: `savings:${debtor.account.address}` });
    debtor.wallet = await world.proxyFor({ address: `wallet:${debtor.account.address}` });
    await transfer(world.simlp, debtor.savings.address, 20n * RAW);
    await transfer(world.simlp, debtor.wallet.address, 5n * RAW);
    await transfer(world.simlp, world.holderAddress(investor.account), 7n * RAW);
    debtor.book_(await world.households.depositBuck(debtor, true));
    investor.book_(await world.households.depositBuck(investor));
    assert.ok(debtor.receipts.length && investor.receipts.length);
    for (const [address, amount] of [[owner, 3n], [debtor.savings.address, 13n * RAW], [debtor.wallet.address, 17n * RAW]]) {
      await s.send(world.usdc, "mint", [address, amount]);
    }
    for (const [i, address, whole] of [[0, owner, 1n], [0, debtor.savings.address, 2n], [1, debtor.wallet.address, 3n]]) {
      await s.send(world.tokens[i].erc20, "mint", [address, whole * 10n ** BigInt(world.tokens[i].dec)]);
    }
    debtor.livingArrears = 19n; debtor.premiumArrears = 23n;
    debtor.hypoLivingArrears = 3n; debtor.hypoPremiumArrears = 4n;
    for (const a of family) a.started = true; // component observations, not an enabled decision loop
    const priceRatio = async (pool, token) => {
      const [sqrt] = await s.call(pool, "slot0");
      return (await s.call(pool, "token0")).toLowerCase() === token.address.toLowerCase()
        ? [sqrt * sqrt, 1n << 192n] : [1n << 192n, sqrt * sqrt];
    };
    const check = async (agent) => {
      const holder = world.holderAddress(agent.account);
      const addresses = [holder, ...[agent.savings, agent.wallet].filter(Boolean).map((p) => p.address)];
      const book = await world.netCreditOps.markHousehold(holder, world._credits.get(agent.account.address) ?? []);
      const holdings = await Promise.all(agent.receipts.map((rid) => s.call(basket, "holdings", [rid])));
      const marks = await Promise.all(agent.receipts.map((rid) => s.call(basket, "valueOf", [rid])));
      const basis = holdings.reduce((sum, h) => sum + h[1], 0n), mark = marks.reduce((sum, v) => sum + v, 0n);
      const cash = (await Promise.all(addresses.map((a) => s.call(world.usdc, "balanceOf", [a])))).reduce((sum, v) => sum + v, 0n);
      const bucks = (await Promise.all(addresses.map((a) => s.call(buck, "netBalanceScaled", [a])))).reduce((sum, v) => sum + v, 0n);
      let pending = 0n;
      for (const token of world.tokens) {
        const amount = (await Promise.all(addresses.map((a) => s.call(token.erc20, "balanceOf", [a])))).reduce((sum, v) => sum + v, 0n);
        const [num, den] = await priceRatio(token.poolUsdc, token.erc20);
        pending += amount * num / den;
      }
      const [num, den] = await priceRatio(world.poolUB, buck);
      const valued = (bucks + mark * Q + book.premiumNetScaled + book.reliefScaled - book.state.issued * Q) * num / (Q * den);
      const block = (await s.client.getBlock()).number;
      const tradeBook = [agent.externalPaid, agent.hypoPaid, agent.basketPrincipal, agent.shares,
        [...agent.book].map(([id, terms]) => [id, { ...terms }])];
      await agent.record(world, 370);
      const row = agent.ledger.at(-1);
      assert.equal((await s.client.getBlock()).number, block);
      assert.equal(row.markBlock, block);
      assert.equal(row.nw, cash + pending + valued - agent.mortgage - agent.livingArrears - agent.premiumArrears);
      assert.equal(row.drawn, book.state.issued);
      assert.equal(row.held, book.state.spendable);
      assert.equal(row.premiumNet, book.premiumNetScaled / Q);
      assert.equal(row.reliefScaled, book.reliefScaled);
      assert.equal(row.basketAtRisk, basis);
      assert.equal(row.basketMark, mark);
      assert.equal(row.basketRoi, basis ? Number(mark - basis) / Number(basis) : null);
      assert.equal(row.pending, pending);
      assert.equal(row.hypo, agent.hypoCash - agent.hypoMortgage - agent.hypoLivingArrears - agent.hypoPremiumArrears);
      assert.deepEqual([agent.externalPaid, agent.hypoPaid, agent.basketPrincipal, agent.shares,
        [...agent.book].map(([id, terms]) => [id, { ...terms }])], tradeBook);
      return row;
    };
    const before = await check(debtor);
    assert.equal(before.auxCash, 30n * RAW);
    assert.ok(before.insuranceAge > 0n && before.nw < 0n && before.pending > 0n);
    assert.equal((await check(saver)).basketRoi, null);
    assert.notEqual((await check(investor)).basketRoi, null);
    const [rid] = debtor.receipts, [shares] = await s.call(basket, "holdings", [rid]);
    await execute(debtor.savings, basket, "redeemShares", [rid, shares / 2n, 0n]);
    assert.ok((await check(debtor)).basketAtRisk < debtor.basketPrincipal, "stale local trade shares do not override actual holdings");
    await s.send(world.usdc, "mint", [owner, 2n ** 100n + 123n]);
    await check(debtor);
    const sourceId = await s.call(credit, "totalSupply");
    await s.send(credit, "createCredit", [world.simlp.address, 0, 2n ** 62n, 0n, 0, 0, 0, 0], { account: world.operator.account });
    await world.netCreditOps.issue(world.simlp.address, sourceId, 2n ** 62n, 2n ** 60n + 17n, 0n,
      "0x0000000000000000000000000000000000000000");
    await transfer(world.simlp, owner, 2n ** 60n + 17n);
    assert.ok((await check(debtor)).held > 2n ** 53n);
    await s.send(wheel, "setValuationSource", [net.fund.address]);
    await assert.rejects(debtor.record(world, 371), /MarkUnavailable|0x417c0238/);
    await s.send(wheel, "refreshValuation");
    await check(debtor);
    const wallet = debtor.wallet;
    debtor.wallet = debtor.savings;
    await assert.rejects(debtor.record(world, 371), /distinct/);
    debtor.wallet = wallet;
  });
  assert.equal(s.mismatches.length, 0);
  assert.throws(() => worldRecord(world), /equilibrium save\/restore/);
});
