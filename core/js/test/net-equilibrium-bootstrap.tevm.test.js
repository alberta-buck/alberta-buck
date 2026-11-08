// The existing equilibrium builder, with actual linked Buck and the same
// basket/fund graph as the wallet stack. Household lifecycle comes next.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { encodeFunctionData, keccak256, toBytes } from "viem";
import { tevmSession } from "../src/backends.js";
import { buildEquilibriumWorld, DayClock } from "../src/scenarios/eqworld.js";
import { artifact } from "../artifacts/bundle.mjs";
import { createInsurerPool, worldRecord } from "../src/buckworld.js";
import { createNetMath } from "../src/net-math.js";
import * as identity from "../src/identity.js";
import { CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent } from "../src/agents/credit-household.js";
import { advanceTime, DAY } from "../src/buckworld.js";
import { buyBuck, sellBuck, netPurchaseFromReceipt, netSaleFromReceipt, tradingOpen } from "../src/market.js";
import { TransactionReceiptPending, TransactionReverted } from "../src/session.js";
import { settlementOps } from "../src/settlement.js";
import { observeNetMarket, netSaleExpected, netPurchaseExpected, netPurchaseCost } from "../src/net-market-quote.js";
import { buyBuckExact } from "../src/market.js";
import { MortgageBook } from "../src/agents/household-calendar.js";

const REFINANCE = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-refinance.json", import.meta.url)));
const CALENDAR = JSON.parse(readFileSync(new URL("../../../test/fixtures/household-calendar.json", import.meta.url))).flow;
const RAW = 1_000_000n;

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
      const a = new Agent({ account, member: `books-${i}`, settings: { home_k: 1000, premium_bp: 100, asset_count: 4 } });
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
  await t.test("one facility per insured asset is the household default", async () => {
    for (const [i, Agent] of [CreditHouseholdAgent, CreditSaverAgent, HouseholdInvestorAgent].entries()) {
      const account = { address: `0x${(0xabc60n + BigInt(i)).toString(16).padStart(40, "0")}` };
      const a = new Agent({ account, member: `default-assets-${i}`, settings: { home_k: 1000, premium_bp: 100 } });
      await a.setup(world);
      const owner = world.holderAddress(account), ids = world._credits.get(account.address) ?? [];
      assert.equal(ids.length, a.investor ? 0 : 1); assert.equal(a.p.asset_count, ids.length);
      if (!a.investor) assert.equal((await s.call(credit, "creditInfo", [ids[0]]))[0], a.face);
      assert.equal((await world.netCreditOps.household(owner, ids)).issued, 0n);
    }
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
    const globalRate = await world.netCreditOps.quoteGlobalReliefRate();
    assert.equal(globalRate.positionMark, publication[1]);
    assert.equal(globalRate.cashScaled, (await s.call(buck, "fundBalancesScaled"))[1]);
    const selected = await world.netCreditOps.quoteReliefRate(await s.call(basket, "creditId"), RAW);
    assert.equal(selected.capped, false);
    assert.ok(globalRate.annualRateWad >= selected.annualRateWad && globalRate.annualRateWad - selected.annualRateWad <= 1n);
    await s.send(wheel, "setValuationSource", [net.fund.address]);
    await assert.rejects(world.netCreditOps.quoteGlobalReliefRate(), /MarkUnavailable|0x417c0238/);
    await s.send(wheel, "refreshValuation");
    assert.ok((await world.netCreditOps.quoteGlobalReliefRate()).positionMark > 0n);
    const basketId = await s.call(basket, "creditId");
    const [principal] = await s.call(credit, "issuanceOf", [basketId]);
    const [relief] = await s.call(net.fund, "previewRelief", [basketId, principal]);
    assert.ok(relief > 0n);
    assert.equal(await s.call(basket, "reliefAccrued"), relief / (10n ** 36n));
    assert.equal(await s.call(world.reg, "isCarrying", [basket.address]), true);
    assert.equal(await s.call(buck, "approvedCarryingIssuer", [basket.address]), true);
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
    let quote = await observeNetMarket(s, buck, world.usdc, world.poolUB, world.fees.ub);
    assert.ok(quote.net_scaled < quote.gross_scaled);
    const sold = await sellBuck(world, world.netMarket, holder, 100n * RAW, { minOut: 90n * RAW });
    assert.ok(sold.received - netSaleExpected(quote, 100n * RAW) <= 4n && netSaleExpected(quote, 100n * RAW) - sold.received <= 4n);
    assert.equal(sold.nominalPaid, 100n * RAW);
    assert.equal(await world.households.cash(account) - before.cash, sold.received);
    assert.equal((await world.netCreditOps.household(owner, ids)).issued, before.drawn);
    assert.equal(await s.call(buck, "totalIssued"), supply);
    assert.equal(debtor.mortgage, initialMortgage);
    assert.equal(debtor.externalPaid, initialPaid, "sale proceeds are not an external-debt payment");
    const receiver = await world.proxyFor({ address: `market-recipient:${account.address}` });
    quote = await observeNetMarket(s, buck, world.usdc, world.poolUB, world.fees.ub);
    const bought = await buyBuck(world, world.netMarket, holder, 10n * RAW,
      { minOut: 9n * RAW, recipient: receiver.address });
    assert.ok(bought.received - netPurchaseExpected(quote, 10n * RAW) <= 4n && netPurchaseExpected(quote, 10n * RAW) - bought.received <= 4n);
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
    quote = await observeNetMarket(s, buck, world.usdc, world.poolUB, world.fees.ub);
    const budget = netPurchaseCost(quote, RAW) * 101n / 100n;
    const exact = await buyBuckExact(world, world.netMarket, holder, RAW, budget, { recipient: receiver.address });
    assert.ok(exact.received >= RAW && exact.paid <= budget);
    await assert.rejects(observeNetMarket(s, buck, world.usdc, world.poolUB, 3000), /fee mismatch/);
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
  await t.test("external insurance follows actual coverage and restores costs after retirement", async () => {
    const account = { address: "0x00000000000000000000000000000000000abe00" };
    const a = new CreditHouseholdAgent({ account, member: "insurance-activation", settings: { home_k: 1000, premium_bp: 100 } });
    await a.setup(world);
    const owner = world.holderAddress(account), [id] = world._credits.get(account.address);
    const full = a.face * 100n / 10000n / 12n;
    assert.equal(await a.externalPremiumDue(world, full), full);
    await s.send(world.usdc, "mint", [owner, 3000n * RAW]);
    await s.send(world.simlp, "exec", [buck.address, encodeFunctionData({ abi: buck.abi,
      functionName: "transfer", args: [owner, 20n * RAW] })]);
    const insurer = await s.call(credit, "insurerOf", [id]);
    await world.netCreditOps.issue(owner, id, 100n * RAW, 40n * RAW, 10n * RAW,
      await s.call(buck, "insurerPool", [insurer]));
    a.joined = true;
    const expected = (full * (a.face - 100n * RAW) + a.face - 1n) / a.face;
    assert.equal(await a.externalPremiumDue(world, full), expected); assert.ok(expected > 0n && expected < full);
    a.incomeAnnual = 0n; a.payment = 0n; a.hypoCash = 2n * full; a.p.living_frac = 0;
    a.tendWallet = async () => {}; // working-wallet migration is a separate calendar gate
    await a.monthly(world);
    assert.equal(a.extPremium, expected); assert.equal(a.hypoPremium, full);
    const plan = await world.netCreditOps.planReduction(owner, id, 25n * RAW);
    assert.equal(plan.reason, "ready"); assert.equal(plan.principal, 25n * RAW);
    await world.netCreditOps.settlePlan(owner, plan, (await s.client.getBlock()).timestamp + 3600n);
    assert.equal((await s.call(credit, "creditInfo", [id]))[1], 50n * RAW);
    const restored = await a.externalPremiumDue(world, full);
    assert.ok(restored > expected);
    await a.monthly(world);
    assert.equal(a.extPremium, expected + restored); assert.equal(a.hypoPremium, 2n * full);
  });
  await t.test("refinance assessment reads exact affordable options before any transaction", async () => {
    for (const [i, [cashK, explicit]] of [[40n, true], [10n, false], [40n, false]].entries()) {
      const account = { address: `0x${(0xabe10n + BigInt(i)).toString(16).padStart(40, "0")}` };
      const a = new CreditHouseholdAgent({ account, member: `assessment-${i}`, settings: { home_k: 1000, premium_bp: 100,
        apr: .06, ltv: .4, theta: 3, risk: 0, switch_k: 0, max_impact_bp: 100, divest_rate: 0 } });
      await a.setup(world); await world.households.openTrading(account);
      const owner = world.holderAddress(account), ids = world._credits.get(account.address);
      await s.send(world.usdc, "mint", [owner, cashK * 1000n * RAW]);
      const before = { block: (await s.client.getBlock()).number, mortgage: a.mortgage, hypo: a.hypoMortgage,
        hypoCash: a.hypoCash, supply: await s.call(buck, "totalIssued") };
      const forecasts = explicit ? { expected_relief_rate: .02, expected_refund_growth: -.02, expected_wallet_growth: -.02 } : {};
      const { plan, verdict, context } = await a.assessNetRefinance(world, 1, forecasts);
      assert.equal((await s.client.getBlock()).number, before.block);
      assert.equal(a.mortgage, before.mortgage); assert.equal(a.hypoMortgage, before.hypo); assert.equal(a.hypoCash, before.hypoCash);
      assert.equal(await s.call(buck, "totalIssued"), before.supply);
      assert.equal(a.refinanceMachine.events.length, 0); assert.equal(a.fundingPending, false);
      if (!explicit) {
        const observed = await world.netCreditOps.quoteGlobalReliefRate();
        assert.equal(context.forecasts.source.block_number, before.block);
        assert.equal(context.forecasts.relief_rate, Number(observed.annualRateWad) / 1e18);
        assert.equal(context.forecasts.basis, "marked-fund/idle-cash");
        assert.ok(Math.abs(context.forecasts.wallet_growth - (Number(math.decay(math.scale, 31_557_600n)) / Number(math.scale) - 1)) < 1e-10);
        assert.equal(context.forecasts.refund_growth, context.forecasts.wallet_growth);
      }
      assert.equal(plan !== null, cashK === 40n); assert.equal(verdict.go, cashK === 40n);
      if (plan === null) {
        assert.ok(context.rejections["cash-buffer"] > 0); assert.equal(context.reason, "no-worthwhile-refinance");
        assert.deepEqual(await s.call(credit, "issuanceOf", [ids[0]]), [0n, 0n]);
        continue;
      }
      assert.ok(plan.candidate && plan.reserve_buy_net > 0n);
      assert.ok(plan.candidate.reasons.every((r) => r === "reserve-shortfall"));
      const expectedSaving = (a.face * 100n / 10000n) * plan.candidate.coverage / a.face;
      assert.ok(Math.abs(verdict.parts.premium - Number(expectedSaving) / 1e6) < 1e-8);
      assert.ok(context.feasible > 1 && context.selected.economics.new_spendable > 0);
      assert.equal(plan.mortgage_limit, plan.minimum_usdc); assert.ok(plan.mortgage_limit <= a.mortgage);
      await a.startNetRefinance(world, plan, 1, {}, { context });
      assert.equal(a.refinanceMachine.status().status, "complete", JSON.stringify({ error: a.lastError,
        phase: a.refinanceMachine.status(), refusal: s.lastRevertReason }, (_, v) => typeof v === "bigint" ? String(v) : v));
      assert.deepEqual(a.householdEventsSince(0).filter((e) => e.status === "confirmed").map((e) => e.operation), ["fund", "issue", "export", "mortgage"]);
      assert.equal(a.mortgage, before.mortgage - plan.mortgage_limit); assert.equal(a.hypoMortgage, before.hypo); assert.equal(a.hypoCash, before.hypoCash);
      assert.ok(await s.call(world.usdc, "balanceOf", [owner]) >= a.buffer);
      // The reserve is ordinary cash. A fixed full-value export also pays
      // demurrage on the cash waiting to be exported; subsequent bank blocks
      // let the remainder decay. Prove cash conservation, not a frozen floor.
      const [issueEvent, saleEvent] = ["issue", "export"].map((op) =>
        a.householdEventsSince().find((e) => e.operation === op && e.status === "confirmed"));
      const [issueTime, saleTime] = await Promise.all([issueEvent, saleEvent].map(async (e) =>
        (await s.client.getBlock({ blockNumber: BigInt(e.block) })).timestamp));
      const beforeSale = BigInt(issueEvent.accounts.buck_scaled);
      assert.ok(beforeSale >= (plan.export_buck + plan.reserve_floor) * math.scale);
      const afterSale = math.decay(beforeSale, saleTime - issueTime) - plan.export_buck * math.scale;
      assert.equal(BigInt(saleEvent.accounts.buck_scaled), afterSale);
      const currentTime = (await s.client.getBlock()).timestamp;
      assert.equal(await s.call(buck, "netBalanceScaled", [owner]), math.decay(afterSale, currentTime - saleTime));
      assert.ok(await s.call(buck, "balanceOf", [owner]) >= plan.candidate.reserveRequired);
    }
  });
  await t.test("available activated credit issues without redundant reserve purchases", async () => {
    for (const [i, keepReserve] of [true, false].entries()) {
      const account = { address: `0x${(0xabe20n + BigInt(i)).toString(16).padStart(40, "0")}` };
      const a = new CreditHouseholdAgent({ account, member: `existing-room-${i}`, settings: { home_k: 1000, premium_bp: 100,
        apr: .06, ltv: .4, theta: 3, risk: 0, switch_k: 0, max_impact_bp: 100, penalty_months: 0, utilization: .0001, divest_rate: 0 } });
      await a.setup(world); await world.households.openTrading(account);
      const owner = world.holderAddress(account), [id] = world._credits.get(account.address), proxy = await world.proxyFor(account);
      await s.send(world.usdc, "mint", [owner, a.buffer]);
      await s.send(world.simlp, "exec", [buck.address, encodeFunctionData({ abi: buck.abi,
        functionName: "transfer", args: [owner, 20n * RAW] })]);
      const insurer = await s.call(credit, "insurerOf", [id]);
      await world.netCreditOps.issue(owner, id, 100n * RAW, 40n * RAW, 10n * RAW, await s.call(buck, "insurerPool", [insurer]));
      a.joined = true;
      if (!keepReserve) await s.send(proxy, "exec", [buck.address, encodeFunctionData({ abi: buck.abi,
        functionName: "transfer", args: [world.simlp.address, await s.call(buck, "balanceOf", [owner])] })]);
      const oldReserve = await s.call(buck, "balanceOf", [owner]);
      await advanceTime(world, 365 * DAY + 6 * 3600);
      await s.send(world.kctrl, "compute"); // normal external control work precedes the read-only decision
      const actualReserve = await s.call(buck, "balanceOf", [owner]);
      assert.ok(keepReserve ? actualReserve < oldReserve : actualReserve === 0n && oldReserve === 0n);
      const book = await world.netCreditOps.household(owner, [id]);
      const assessed = await a.assessNetRefinance(world, 1, { expected_relief_rate: .02, expected_refund_growth: -.02,
        expected_wallet_growth: -.02, reserve_floor: oldReserve });
      const { plan, context } = assessed;
      assert.ok(plan !== null); assert.equal(plan.candidate.coverage, 0n); assert.equal(plan.candidate.premium, 0n);
      assert.equal(plan.reserve_buy_net, 0n); assert.equal(plan.reserve_buy_max_usdc, 0n); assert.equal(plan.reserve_floor, actualReserve);
      assert.ok(plan.export_buck > 0n);
      await a.startNetRefinance(world, plan, 1, {}, { context });
      assert.equal(a.refinanceMachine.status().status, "complete", JSON.stringify({ error: a.lastError,
        phase: a.refinanceMachine.status(), refusal: s.lastRevertReason }, (_, v) => typeof v === "bigint" ? String(v) : v));
      assert.equal((await world.netCreditOps.household(owner, [id])).issued, book.issued + plan.export_buck);
      assert.ok(await s.call(buck, "balanceOf", [owner]) >= actualReserve);
    }
  });
  await t.test("household refinancing commits receipt-backed phases and exact public histories", async (t) => {
    const b = Object.fromEntries(Object.entries(REFINANCE.base).map(([k, v]) => [k, BigInt(v)]));
    for (const [index, vector] of REFINANCE.cases.entries()) await t.test(vector.name, async () => {
      const account = { address: `0x${(0xabd00n + BigInt(index)).toString(16).padStart(40, "0")}` };
      const a = new CreditHouseholdAgent({ account, member: `refinance-vectors-${index}`, settings: { home_k: 1000, premium_bp: 100 } });
      await a.setup(world);
      const owner = world.holderAddress(account), ids = world._credits.get(account.address), machine = a.refinanceMachine;
      await world.households.openTrading(account);
      await s.send(world.usdc, "mint", [owner, BigInt(vector.seed_usdc ?? b.seed_usdc)]);
      const seedBuck = BigInt(vector.seed_buck ?? b.seed_buck);
      if (seedBuck) await s.send(world.simlp, "exec", [buck.address, encodeFunctionData({ abi: buck.abi,
        functionName: "transfer", args: [owner, seedBuck] })]);
      const candidate = await world.netCreditOps.assessIssueCandidate(owner, ids[0], b.coverage, b.new_spendable, b.premium);
      assert.deepEqual(candidate.reasons, vector.reserve_buy_net ? ["reserve-shortfall"] : []);
      const plan = { candidate, ...Object.fromEntries(["export_buck", "minimum_usdc", "mortgage_limit", "cash_buffer",
        "reserve_floor", "switch_cost", "penalty_numerator", "penalty_denominator"].map((k) => [k, b[k]])),
        reserve_buy_net: BigInt(vector.reserve_buy_net ?? 0), reserve_buy_max_usdc: BigInt(vector.reserve_buy_max_usdc ?? 0) };
      if (vector.fault === "revert" && vector.phase === "export") plan.minimum_usdc = 5_000_000_000n;
      if (vector.name === "confirmed") {
        const head = (await s.client.getBlock()).number;
        for (const [changed, reason] of [[{ reserve_floor: 0n }, /quoted BUCK reserve/], [{ cash_buffer: 0n }, /bounds/],
          [{ export_buck: 100_000_000n }, /consume the BUCK reserve/], [{ mortgage_limit: 100_000_000n }, /bounds/],
          [{ reserve_buy_net: RAW }, /reserve funding/], [{ reserve_buy_max_usdc: RAW }, /reserve funding/],
          [{ candidate: null, switch_cost: 0n, reserve_buy_net: RAW, reserve_buy_max_usdc: RAW }, /reserve funding/],
          [{ reserve_buy_net: RAW, reserve_buy_max_usdc: b.seed_usdc }, /USDC buffer/],
          [{ penalty_numerator: (1n << 256n) - 1n, penalty_denominator: 1n }, /overflows uint256/]]) {
          await assert.rejects(a.startNetRefinance(world, { ...plan, ...changed }, 0), reason);
        }
        a.netRepaymentReserve = 1_000_000n; a.netRepaymentTarget = 1_000_000n;
        await assert.rejects(a.startNetRefinance(world, plan, 0), /buyback must finish/);
        a.netRepaymentReserve = null; a.netRepaymentTarget = null;
        assert.equal((await s.client.getBlock()).number, head); assert.equal(machine.events.length, 0);
      }
      const before = { mortgage: a.mortgage, hypo: a.hypoMortgage, hypoCash: a.hypoCash, paid: a.externalPaid,
        premium: a.premiumPaid, switch: a.switchPaid, penalty: a.penaltyPaid, refis: a.refis,
        issued: (await world.netCreditOps.household(owner, ids)).issued, supply: await s.call(buck, "totalIssued"),
        bank: await s.call(world.usdc, "balanceOf", ["0x000000000000000000000000000000000000dEaD"]), seq: machine.events.length };
      const calls = {}, originalIssue = world.netCreditOps.issue, originalExecute = world.netMarket.execute;
      const originalPay = world.netSettlementOps.payExact, originalAssess = world.netCreditOps.assessIssueCandidate;
      let readFailures = 0;
      world.netCreditOps.assessIssueCandidate = async (...args) => {
        if (vector.fault === "quote-unavailable" && readFailures++ === 0) throw new Error("quote endpoint unavailable after funded purchase");
        const fresh = await originalAssess(...args);
        return vector.fault === "quote-changed" ? { ...fresh, premium: fresh.premium + 1n } : fresh;
      };
      const bankOps = settlementOps(s, world.usdc, { execute: (who, c, name, args) =>
        originalExecute(who, c, name, args, { expect: "either" }) });
      const intercept = (fn, receiptOf = (r) => r) => async (...args) => {
        const phase = machine.flow.phase;
        calls[phase] = (calls[phase] ?? 0) + 1;
        if (phase === vector.phase && vector.fault === "unknown") throw new Error("submission outcome unknown");
        const result = await fn(...args), receipt = receiptOf(result);
        if (phase === vector.phase && vector.fault === "lost") throw new TransactionReceiptPending(receipt.transactionHash);
        return result;
      };
      world.netCreditOps.issue = intercept(originalIssue);
      world.netMarket.execute = intercept(originalExecute);
      world.netSettlementOps.payExact = intercept((who, bank, amount) => bankOps.payExact(who, bank,
        vector.fault === "revert" && vector.phase === "mortgage" ? (1n << 256n) - 1n : amount), (r) => r.receipt);
      try {
        const ctr = {};
        await a.startNetRefinance(world, plan, 0, ctr, { context: { reason: "shared execution vector", wide: 2n ** 100n + 7n } });
        if (vector.fault === "lost") {
          assert.equal(machine.status().status, "pending"); assert.equal(machine.status().phase, vector.phase);
          assert.equal(a.externalPaid, before.paid); assert.equal(a.switchPaid, before.switch); assert.equal(a.penaltyPaid, before.penalty);
          const sent = { ...calls }, actualReceipt = s.client.getTransactionReceipt;
          s.client.getTransactionReceipt = async (params) => ({ ...await actualReceipt(params), transactionHash: `0x${"00".repeat(32)}` });
          try { await a.act(world, 1, 0); } finally { s.client.getTransactionReceipt = actualReceipt; }
          assert.equal(machine.status().status, "pending"); assert.deepEqual(calls, sent);
          assert.equal(a.mortgage, before.mortgage);
          await a.act(world, 2, 0);
        } else if (vector.fault === "unknown") {
          const sent = { ...calls };
          await a.act(world, 1, 0); await a.act(world, 2, 0);
          assert.deepEqual(calls, sent); assert.match(a.lastError, /hash unavailable/);
        } else if (vector.fault === "quote-unavailable") {
          assert.equal(machine.status().phase, "issue"); assert.equal(machine.status().status, "running");
          assert.deepEqual(calls, { fund: 1 }); assert.equal(a.externalPaid, before.paid); assert.equal(a.premiumPaid, before.premium);
          await a.act(world, 1, 0);
        }
        assert.equal(machine.status().status, vector.final);
        assert.ok(Object.values(calls).every((n) => n === 1));
        const complete = vector.final === "complete", principal = complete ? b.mortgage_limit : 0n;
        const expected = vector.confirmed ?? (complete ? ["issue", "export", "mortgage"]
          : vector.fault === "unknown" ? [] : vector.phase === "export" ? ["issue"] : ["issue", "export"]);
        const issued = expected.includes("issue") ? b.new_spendable + b.premium : 0n;
        const penalty = (principal * b.penalty_numerator + b.penalty_denominator - 1n) / b.penalty_denominator;
        assert.equal(a.mortgage, before.mortgage - principal); assert.equal(a.externalPaid, before.paid + principal);
        assert.equal(a.hypoMortgage, before.hypo); assert.equal(a.hypoCash, before.hypoCash);
        assert.equal(a.premiumPaid, before.premium + (expected.includes("issue") ? b.premium : 0n));
        assert.equal(a.switchPaid, before.switch + (complete ? b.switch_cost : 0n)); assert.equal(a.penaltyPaid, before.penalty + penalty);
        assert.equal(a.refis, before.refis + (complete ? 1 : 0));
        assert.equal(ctr.rtrUsdRetired ?? 0n, principal / 1_000_000n);
        assert.equal(await s.call(world.usdc, "balanceOf", ["0x000000000000000000000000000000000000dEaD"]),
          before.bank + principal + (complete ? b.switch_cost : 0n) + penalty);
        assert.equal((await world.netCreditOps.household(owner, ids)).issued, before.issued + issued);
        assert.equal(await s.call(buck, "totalIssued"), before.supply + issued);
        assert.ok(await s.call(buck, "balanceOf", [owner]) >= (seedBuck || expected.includes("fund") ? b.reserve_floor : 0n));
        const events = JSON.parse(JSON.stringify(a.householdEventsSince(before.seq)));
        assert.equal(events[0].context.wide, String(2n ** 100n + 7n));
        assert.equal(events[0].limits.theta, a.p.theta); assert.equal(events[0].plan.export_buck, String(b.export_buck));
        assert.ok(BigInt(events[0].market.k_wad) > 0n);
        assert.equal(events[0].accounts.ratios.reserve_cover, Number(seedBuck) / Number(b.reserve_floor));
        assert.ok(events.every((e) => /^\d+$/.test(e.accounts.usdc)));
        const confirmed = events.filter((e) => e.status === "confirmed");
        assert.deepEqual(confirmed.map((e) => e.operation), expected);
        if (expected.includes("fund")) {
          const amounts = confirmed.find((e) => e.operation === "fund").amounts;
          assert.ok(BigInt(amounts.usdc) > 0n && BigInt(amounts.usdc) <= plan.reserve_buy_max_usdc);
          assert.ok(BigInt(amounts.net_buck_scaled) >= plan.reserve_buy_net * 10n ** 36n);
          const bank = confirmed.filter((e) => e.operation === "mortgage").reduce((sum, e) => sum + BigInt(e.amounts.usdc), 0n);
          const proceeds = confirmed.filter((e) => e.operation === "export").reduce((sum, e) => sum + BigInt(e.amounts.usdc), 0n);
          assert.equal(await s.call(world.usdc, "balanceOf", [owner]), BigInt(vector.seed_usdc ?? b.seed_usdc) - BigInt(amounts.usdc) + proceeds - bank);
        }
        assert.throws(() => a.householdEventsSince(-1), /cursor/);
        if (!machine.active) assert.equal(await machine.advance(world, 3), false);
      } finally {
        world.netCreditOps.issue = originalIssue; world.netMarket.execute = originalExecute; world.netSettlementOps.payExact = originalPay;
        world.netCreditOps.assessIssueCandidate = originalAssess;
      }
      if (vector.final === "pending") {
        // Unknown-hash cases intentionally remain stopped; isolate the next
        // vector's household instead of clearing a live operation for reuse.
        assert.equal(machine.status().status, "pending");
      }
    });
  });
  await t.test("household calendar recovers actual flows without stopping the shadow", async (t) => {
    const phases = ["income", "living", "insurance", "mortgage"], BANK = "0x000000000000000000000000000000000000dEaD";
    for (const [index, c] of CALENDAR.cases.entries()) await t.test(c.name, async () => {
      const account = { address: `0x${(0xbca00n + BigInt(index)).toString(16).padStart(40, "0")}` };
      const a = new CreditHouseholdAgent({ account, member: `calendar-${index}`, settings: CALENDAR.settings });
      await a.setup(world);
      const m = a.calendarMachine, owner = world.holderAddress(account), initialDebt = a.mortgage;
      const [opening, income, living, premium] = ["opening", "income", "living", "full_premium"].map((k) => BigInt(CALENDAR[k]));
      const bankBefore = await s.call(world.usdc, "balanceOf", [BANK]);
      assert.equal(await a.advanceNetCalendar(world, 0), false);
      assert.equal(a.started, true); assert.equal(a.hypoCash, opening);
      assert.equal(await s.call(world.usdc, "balanceOf", [owner]), opening);
      assert.equal(a.incomeTotal + a.externalPaid + m.incomeDue, 0n);
      const calls = {}, mint = m.mint.bind(m), pay = world.netSettlementOps.payExact, record = m.recordMortgage.bind(m);
      let failed = false;
      const submit = async (phase, dispatch, receiptOf, ...args) => {
        calls[phase] = (calls[phase] ?? 0) + 1;
        if (c.phase === phase && c.fault === "unknown") throw new Error("submission outcome unknown");
        if (phase === "income" && c.fault === "bad-mint-evidence") args = [args[0], args[1] + 1n];
        if (phase === "income" && c.fault === "revert" && !failed) {
          failed = true;
          return s.send(world.usdc, "transfer", [BANK, (1n << 256n) - 1n], { expect: "either" });
        }
        const result = await dispatch(...args), receipt = receiptOf(result);
        if (c.phase === phase && c.fault === "lost" && !failed) {
          failed = true; throw new TransactionReceiptPending(receipt.transactionHash);
        }
        return result;
      };
      m.mint = (...args) => submit("income", mint, (r) => r, ...args);
      world.netSettlementOps.payExact = (...args) => submit(m.flow.phase, pay, (r) => r.receipt, ...args);
      m.recordMortgage = async (...args) => {
        if (c.fault === "date-unavailable" && !failed) { failed = true; throw new Error("receipt block temporarily unavailable"); }
        return record(...args);
      };
      const moveTo = async (day) => advanceTime(world, m.epoch + BigInt(day * DAY) - (await s.client.getBlock()).timestamp);
      try {
        await moveTo(30);
        const pending = await a.advanceNetCalendar(world, 30);
        assert.equal(pending, c.fault !== "none", a.lastError);
        assert.equal(a.hypoPaid, a.payment); assert.equal(a.hypoLiving, living); assert.equal(a.hypoPremium, premium);
        assert.equal(a.hypoCash, opening + income - living - premium - a.payment);
        const sent = { ...calls };
        if (pending) {
          assert.equal(m.status().status, c.fault === "revert" ? "refused" : "pending"); assert.equal(m.flow.phase, c.phase);
          await assert.rejects(a.startNetRefinance(world, null, 30), /calendar must finish/);
          if (["unknown", "bad-mint-evidence"].includes(c.fault)) {
            assert.equal(m.incomeDue, income); assert.equal(a.incomeTotal, 0n);
            await moveTo(60); await a.act(world, 60, 0);
            assert.deepEqual(calls, sent); assert.equal(a.incomeTotal + a.externalPaid, 0n);
            assert.equal(await s.call(world.usdc, "balanceOf", [owner]), opening + (c.fault === "bad-mint-evidence" ? income + 1n : 0n));
            assert.equal(m.incomeDue, 2n * income); assert.equal(m.queue.length, 2);
            assert.equal(a.hypoPaid, 2n * a.payment); assert.ok(a.hypoInterest > 0n);
            assert.equal(a.livingArrears, 2n * living); assert.equal(a.premiumArrears, 2n * premium);
            assert.match(a.lastError, c.fault === "unknown" ? /hash unavailable/ : /does not match terms/); return;
          }
          assert.ok(m.flow.hash);
          if (c.phase === "mortgage") { assert.equal(a.externalPaid, 0n); assert.equal(m.status().mortgage_provisional, true); }
          const getReceipt = s.client.getTransactionReceipt;
          if (c.fault !== "revert") {
            s.client.getTransactionReceipt = async (args) => ({ ...await getReceipt(args), transactionHash: `0x${"00".repeat(32)}` });
            try { assert.equal(await a.advanceNetCalendar(world, 30), true); }
            finally { s.client.getTransactionReceipt = getReceipt; }
          }
          assert.deepEqual(calls, sent);
          await moveTo(35); await a.act(world, 35, 0);
        }
        const expectedCalls = Object.fromEntries(phases.map((k) => [k, 1]));
        expectedCalls.income += c.fault === "revert" ? 1 : 0;
        assert.deepEqual(calls, expectedCalls);
        assert.equal(a.externalPaid, a.payment); assert.equal(a.hypoPaid, a.payment);
        assert.equal(a.incomeTotal, income); assert.equal(a.livingPaid, living); assert.equal(a.extPremium, premium);
        assert.equal(a.livingArrears + a.premiumArrears + m.incomeDue, 0n);
        assert.equal(await s.call(world.usdc, "balanceOf", [BANK]) - bankBefore, living + premium + a.payment);
        assert.equal(await s.call(world.usdc, "balanceOf", [owner]), opening + income - living - premium - a.payment);
        const bankDay = pending && c.phase !== "mortgage" ? 35 : 30;
        const actual = new MortgageBook(initialDebt, 60_000_000n);
        actual.advance(bankDay); actual.recordPayment("bank", bankDay, a.payment); actual.advance(m.day);
        assert.deepEqual(m.actual.state, actual.state);
        const shadow = new MortgageBook(initialDebt, 60_000_000n);
        shadow.advance(30); shadow.recordPayment("shadow", 30, a.payment); shadow.advance(m.day);
        assert.deepEqual(m.shadow.state, shadow.state); assert.equal(m.status().mortgage_provisional, false);
        const events = a.householdEventsSince(), count = events.length;
        assert.ok(events.every((e) => e.kind === "calendar" && Object.hasOwn(e.accounts, "counterfactual_mortgage")));
        assert.equal(Number(events.find((e) => e.operation === "mortgage" && e.status === "confirmed").effective_day), bankDay);
        assert.equal(await a.advanceNetCalendar(world, m.day), false);
        assert.equal(a.householdEventsSince().length, count);
        await moveTo(60);
        assert.equal(await a.advanceNetCalendar(world, 60), false);
        assert.equal(a.externalPaid, 2n * a.payment); assert.equal(a.hypoPaid, 2n * a.payment);
        assert.equal(a.incomeTotal, 2n * income);
        assert.deepEqual(calls, Object.fromEntries(Object.entries(expectedCalls).map(([k, v]) => [k, v + 1])));
      } finally { world.netSettlementOps.payExact = pay; }
    });
  });
  await t.test("calendar charges only unactivated external insurance", async () => {
    const account = { address: "0x00000000000000000000000000000000000bcaf0" };
    const a = new CreditHouseholdAgent({ account, member: "calendar-insurance", settings: CALENDAR.settings });
    await a.setup(world); await a.advanceNetCalendar(world, 0);
    const owner = world.holderAddress(account), [id] = world._credits.get(account.address);
    await s.send(world.simlp, "exec", [buck.address, encodeFunctionData({ abi: buck.abi, functionName: "transfer", args: [owner, 20n * RAW] })]);
    const insurer = await s.call(credit, "insurerOf", [id]);
    await world.netCreditOps.issue(owner, id, 100n * RAW, 40n * RAW, 10n * RAW, await s.call(buck, "insurerPool", [insurer]));
    const full = BigInt(CALENDAR.full_premium), expected = (full * (a.face - 100n * RAW) + a.face - 1n) / a.face;
    await advanceTime(world, a.calendarMachine.epoch + BigInt(30 * DAY) - (await s.client.getBlock()).timestamp);
    assert.equal(await a.advanceNetCalendar(world, 30), false, a.lastError);
    assert.equal(a.extPremium, expected); assert.ok(expected < full); assert.equal(a.hypoPremium, full);
    assert.equal(a.mortgage, a.hypoMortgage);
  });
  await t.test("calendar dates refinance receipts and keeps its paydays", async (t) => {
    for (const [index, fault] of ["lost", "date-unavailable"].entries()) await t.test(fault, async () => {
      const account = { address: `0x${(0xbcb00n + BigInt(index)).toString(16).padStart(40, "0")}` };
      const a = new CreditHouseholdAgent({ account, member: `calendar-refinance-${index}`, settings: CALENDAR.settings });
      await a.setup(world); await a.advanceNetCalendar(world, 0); await world.households.openTrading(account);
      const m = a.calendarMachine, initial = a.mortgage, owner = world.holderAddress(account), [id] = world._credits.get(account.address);
      const b = Object.fromEntries(Object.entries(REFINANCE.base).map(([k, v]) => [k, BigInt(v)]));
      await s.send(world.simlp, "exec", [buck.address, encodeFunctionData({ abi: buck.abi, functionName: "transfer", args: [owner, b.seed_buck] })]);
      await s.send(world.kctrl, "compute");
      const candidate = await world.netCreditOps.assessIssueCandidate(owner, id, b.coverage, b.new_spendable, b.premium);
      assert.deepEqual(candidate.reasons, []);
      const plan = { candidate, ...Object.fromEntries(["export_buck", "minimum_usdc", "mortgage_limit", "cash_buffer",
        "reserve_floor", "switch_cost", "penalty_numerator", "penalty_denominator"].map((k) => [k, b[k]])) };
      const pay = world.netSettlementOps.payExact, record = m.recordMortgage.bind(m), sends = [];
      let dates = 0;
      world.netSettlementOps.payExact = async (...args) => {
        const result = await pay(...args); sends.push(result.receipt);
        if (fault === "lost" && sends.length === 1) throw new TransactionReceiptPending(result.receipt.transactionHash);
        return result;
      };
      m.recordMortgage = async (...args) => {
        if (fault === "date-unavailable" && dates++ === 0) throw new Error("receipt block temporarily unavailable");
        return record(...args);
      };
      try {
        await a.startNetRefinance(world, plan, 0);
        assert.equal(a.refinanceMachine.status().status, "pending", a.lastError);
        assert.ok(a.refinanceMachine.flow.hash); assert.equal(m.status().mortgage_provisional, true);
        assert.equal(a.externalPaid, 0n);
        const receipt = sends[0], block = await s.client.getBlock({ blockNumber: receipt.blockNumber });
        await advanceTime(world, m.epoch + BigInt(30 * DAY) - (await s.client.getBlock()).timestamp);
        await a.act(world, 30, 0);
        assert.equal((await s.client.getBlock({ blockNumber: receipt.blockNumber })).timestamp, block.timestamp);
        assert.equal(a.externalPaid, b.mortgage_limit); assert.equal(sends.length, 1);
        assert.equal(a.refinanceMachine.status().status, "complete");
        assert.equal(a.incomeTotal, 0n); assert.equal(m.incomeDue, BigInt(CALENDAR.income)); assert.equal(a.hypoPaid, a.payment);
        const expected = new MortgageBook(initial, 60_000_000n);
        expected.recordPayment("refinance", 0, b.mortgage_limit); expected.advance(30);
        assert.deepEqual(m.actual.state, expected.state);
        await a.act(world, 30, 0);
        assert.equal(a.externalPaid, b.mortgage_limit + a.payment); assert.equal(sends.length, 4);
        assert.equal(a.switchPaid, b.switch_cost); assert.equal(a.refis, 1); assert.equal(m.active, false); assert.equal(m.incomeDue, 0n);
      } finally { world.netSettlementOps.payExact = pay; }
    });
  });
  await t.test("calendar refuses skipped paydays and labels without chain time", async () => {
    const a = new CreditHouseholdAgent({ account: { address: "0x00000000000000000000000000000000000bcb10" },
      member: "calendar-clock", settings: CALENDAR.settings });
    await a.setup(world); await a.advanceNetCalendar(world, 0);
    const before = [a.mortgage, a.hypoCash, a.hypoPaid, a.livingArrears, a.householdEventsSince().length];
    await assert.rejects(a.advanceNetCalendar(world, 30), /chain clock/);
    assert.deepEqual([a.mortgage, a.hypoCash, a.hypoPaid, a.livingArrears, a.householdEventsSince().length], before);
    await advanceTime(world, a.calendarMachine.epoch + BigInt(31 * DAY) - (await s.client.getBlock()).timestamp);
    await assert.rejects(a.advanceNetCalendar(world, 31), /each payday/);
    assert.deepEqual([a.mortgage, a.hypoCash, a.hypoPaid, a.livingArrears, a.householdEventsSince().length], before);
  });
  await t.test("recurring cash flows run after buyback reconciliation with an open target", async () => {
    const account = { address: "0x00000000000000000000000000000000000bcb20" };
    const a = new CreditHouseholdAgent({ account, member: "calendar-buyback", settings: CALENDAR.settings });
    await a.setup(world); await a.advanceNetCalendar(world, 0); await world.households.openTrading(account);
    const owner = world.holderAddress(account), [id] = world._credits.get(account.address);
    await s.send(world.simlp, "exec", [buck.address, encodeFunctionData({ abi: buck.abi,
      functionName: "transfer", args: [owner, 20n * RAW] })]);
    await s.send(world.kctrl, "compute");
    const insurer = await s.call(credit, "insurerOf", [id]);
    await world.netCreditOps.issue(owner, id, 100n * RAW, 40n * RAW, 10n * RAW, await s.call(buck, "insurerPool", [insurer]));
    const beforeBuck = await s.call(buck, "balanceOf", [owner]), execute = world.netMarket.execute;
    let sends = 0;
    world.netMarket.execute = async (...args) => {
      const receipt = await execute(...args); sends++;
      throw new TransactionReceiptPending(receipt.transactionHash);
    };
    try { await assert.rejects(a.buyForRepayment(world, 2n * RAW, RAW, 25n * RAW), TransactionReceiptPending); }
    finally { world.netMarket.execute = execute; }
    assert.equal(a.netTradeUncertain, true); assert.ok(a.netPendingBuyHash);
    const supply = await s.call(buck, "totalIssued"), reserve = a.netRepaymentReserve;
    await advanceTime(world, a.calendarMachine.epoch + BigInt(30 * DAY) - (await s.client.getBlock()).timestamp);
    await a.act(world, 30, 0);
    assert.equal(a.netTradeUncertain, false); assert.equal(a.netRepaymentTarget, 25n * RAW);
    assert.equal(a.incomeTotal, 0n); assert.equal(a.calendarMachine.active, true);
    await a.act(world, 30, 0);
    assert.equal(a.incomeTotal, BigInt(CALENDAR.income)); assert.equal(a.externalPaid, a.payment);
    assert.equal(a.calendarMachine.active, false); assert.equal(a.netRepaymentTarget, 25n * RAW);
    assert.equal(a.netRepaymentReserve, reserve); assert.equal(sends, 1);
    assert.equal(await s.call(buck, "totalIssued"), supply);
    assert.ok(await s.call(buck, "balanceOf", [owner]) > beforeBuck - RAW);
    const bank = a.householdEventsSince().find((e) => e.operation === "mortgage" && e.status === "confirmed");
    assert.equal(bank.amounts.usdc, String(a.payment)); assert.equal(Number(bank.effective_day), 30);
  });
  await t.test("absolute clock dates different household arrivals consistently", async () => {
    const make = async (member, arrive_day) => {
      const a = new CreditHouseholdAgent({ account: { address: `0x${(0xbd000n + BigInt(arrive_day)).toString(16).padStart(40, "0")}` },
        member, arrive_day, settings: CALENDAR.settings });
      await a.setup(world); return a;
    };
    const a = await make("clock-first", 0), b = await make("clock-later", 1), clock = new DayClock();
    for (const day of [0, 1, 29, 30, 31]) {
      await advanceTime(world, 601); // the existing wheel's inter-tick settlement work
      await clock.act(world, day, 0);
      assert.equal((await s.client.getBlock()).timestamp - world.householdEpoch, BigInt(day * DAY));
      await a.advanceNetCalendar(world, day);
      await advanceTime(world, 7); // another actor can work before the later household binds
      if (day >= b.arrive_day) await b.advanceNetCalendar(world, day);
    }
    assert.equal(a.calendarMachine.epoch, world.householdEpoch); assert.equal(b.calendarMachine.epoch, world.householdEpoch);
    for (const [agent, paidDay] of [[a, 30], [b, 31]]) {
      assert.equal(agent.incomeTotal, BigInt(CALENDAR.income)); assert.equal(agent.externalPaid, agent.payment); assert.equal(agent.hypoPaid, agent.payment);
      assert.deepEqual([...agent.calendarMachine.actual.payments.values()].map((p) => p.day), [paidDay]);
    }
    assert.ok(a.externalInterest > b.externalInterest);
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
