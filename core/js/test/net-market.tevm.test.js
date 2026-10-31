// Real V3 pool and real identity approvals with the cash-only net world.
// This does not model the multi-asset basket or a five-year economy.
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { getContractAddress } from "viem";
import { devAccount, tevmSession } from "../src/backends.js";
import { buildBuckWorld, onboard, insureAsset, advanceTime, DAY } from "../src/buckworld.js";
import { netCreditOps } from "../src/net-world.js";
import { CreditHouseholdAgent } from "../src/agents/credit-household.js";
import { netHouseholdRepaymentOps } from "../src/agents/net-repayment.js";
import { Q96 } from "../src/v3.js";
import { TransactionReceiptPending, TransactionReverted } from "../src/session.js";
import { buildMarket, marketRecord, attachMarket, attachNetMarket, openTrading, fiatIn, buyBuck, buyBuckExact, sellBuck,
  netPurchaseFromReceipt }
  from "../src/market.js";

let identity;
try { identity = await import("../src/identity.js"); } catch { /* node kernel not built */ }
const bundle = new URL("../artifacts/sandbox.mjs", import.meta.url);
const skip = !identity ? "identity kernel not built (make nix-core-build-wasm)"
  : !existsSync(bundle) ? "sandbox artifacts not built (make nix-venv-net-demurrage-smoke)" : false;
const fields = (name) => ({ given_name: name, family_name: "Market", jurisdiction: "Alberta, Canada",
  id_type: "Alberta Identity", id_number: `NET-MARKET-${name}`, date_of_birth: "1990-01-01",
  issued_at: "2026-01-01T00:00:00Z", epoch: 42 });

test("aged carrying V3 output is guarded and reported in net BUCK in both token orders", { skip }, async () => {
  const { artifact } = await import(bundle);
  let seed = 0x109ab42n;
  const rng = () => {
    seed = (seed * 6364136223846793005n + 1442695040888963407n) & ((1n << 256n) - 1n);
    return seed % (identity.ORDER - 1n) + 1n;
  };
  const s = await tevmSession();
  const world = await buildBuckWorld(s, artifact, { identity, rng, accounting: "net" });
  const operator = await onboard(world, s.account, fields("Operator"), { rng });
  let lastMarket, lastBuyer;
  for (const [index, buckIs0] of [true, false].entries()) {
    // buildMarket's first deployment is USDC. Adjust the deployer's nonce so
    // its deterministic address exercises each canonical V3 ordering.
    let aligned = false;
    for (let i = 0; i < 32; i++) {
      const nonce = await s.client.getTransactionCount({ address: s.account.address });
      const next = getContractAddress({ from: s.account.address, nonce: BigInt(nonce) });
      if ((BigInt(world.buck.address) < BigInt(next)) === buckIs0) { aligned = true; break; }
      await s.deploy(artifact("MockERC20"), ["Nonce filler", "FILL", 6], { name: "NonceFiller" });
    }
    assert.ok(aligned, "could not construct requested token ordering");
    const market = await buildMarket(world, artifact, { operator, depth: 1_000_000_000n });
    const recorded = marketRecord(market);
    assert.throws(() => attachMarket(s, artifact, recorded), /attachNetMarket/);
    const attached = await attachNetMarket(s, artifact, recorded, world);
    assert.equal(attached.guard.address.toLowerCase(), market.guard.address.toLowerCase());
    const forgedGuard = structuredClone(recorded);
    forgedGuard.addresses.guard = market.pool.address;
    await assert.rejects(attachNetMarket(s, artifact, forgedGuard, world), /guard code mismatch/);
    assert.equal((await s.call(market.pool, "token0")).toLowerCase() === world.buck.address.toLowerCase(), buckIs0);
    assert.ok(market.providerCreditId !== null);
    const provider = await s.call(world.credit, "issuanceOf", [market.providerCreditId]);
    assert.ok(provider[0] > 1_000_000_000n);
    assert.equal(await s.call(world.reg, "isCarrying", [market.pool.address]), true);
    const buyer = await onboard(world, devAccount(index + 1), fields(`Buyer${index}`), { rng });
    lastMarket = market;
    lastBuyer = buyer;
    await fiatIn(world, market, buyer.account.address, 500_000_000n);
    await openTrading(world, market, buyer, { rng });
    const buyerId = await insureAsset(world, world.operator.account, buyer, { face: 200_000_000n, premiumRate: 0 });
    const creditOps = netCreditOps(s, world.net);
    await creditOps.issue(buyer.account, buyerId, 200_000_000n, 100_000_000n);
    const sale = await sellBuck(world, market, buyer, 99_000_000n);
    assert.ok(sale.received > 0n);
    assert.equal((await creditOps.facility(buyerId)).issued, 100_000_000n);
    await advanceTime(world, 365 * DAY + 6 * 3600);
    const poolNominal = await s.call(world.buck, "balanceOf", [market.pool.address]);
    const poolNet = await s.call(world.buck, "netBalanceOf", [market.pool.address]);
    assert.ok(poolNet < poolNominal);
    assert.ok(await s.call(world.buck, "balanceOf", [buyer.account.address]) < 2_000_000n);
    const result = await buyBuck(world, market, buyer, 100_000_000n, { minOut: 80_000_000n });
    assert.equal(result.paid, 100_000_000n);
    assert.ok(result.received > 0n && result.received < result.nominalReceived);
    assert.ok(await s.call(world.buck, "balanceOf", [buyer.account.address]) >= result.received);
    assert.equal((await s.call(world.credit, "issuanceOf", [market.providerCreditId]))[0], provider[0]);
    assert.equal((await creditOps.facility(buyerId)).issued, 100_000_000n);
    const deadline = (await s.client.getBlock()).timestamp + 3600n;
    const plan = await creditOps.planReduction(buyer.account, buyerId, 10_000_000n);
    assert.equal(plan.reason, "ready");
    assert.equal(plan.principal, 10_000_000n);
    const settled = await creditOps.settlePlan(buyer.account, plan, deadline);
    assert.equal(settled.principal, 10_000_000n);
    assert.ok(settled.reliefScaled > 0n && settled.reliefScaled < settled.principal * 10n ** 36n);
    assert.equal(settled.ownerCostScaled + settled.reliefScaled, settled.principal * 10n ** 36n);
    assert.equal((await creditOps.facility(buyerId)).issued, 90_000_000n);
    const beforeExact = await s.call(market.usdc, "balanceOf", [buyer.account.address]);
    const exact = await buyBuckExact(world, market, buyer, 20_000_000n, 40_000_000n);
    assert.ok(exact.received >= 20_000_000n);
    assert.ok(exact.nominalReceived > exact.received && exact.paid < 40_000_000n);
    assert.equal(await s.call(market.usdc, "balanceOf", [buyer.account.address]), beforeExact - exact.paid);
    const usdBefore = await s.call(market.usdc, "balanceOf", [buyer.account.address]);
    const refused = await s.send(market.guard, "buyExactInput",
      [market.pool.address, 100_000_000n, 200_000_000n, buyer.account.address, 0n],
      { account: buyer.account, expect: "revert" });
    assert.equal(refused.status, "reverted");
    assert.equal(await s.call(market.usdc, "balanceOf", [buyer.account.address]), usdBefore);
    const tooExpensive = await s.send(market.guard, "buyExactOutput",
      [market.pool.address, 20_000_000n, 1_000_000n, buyer.account.address, 0n],
      { account: buyer.account, expect: "revert" });
    assert.equal(tooExpensive.status, "reverted");
    assert.equal(await s.call(market.usdc, "balanceOf", [buyer.account.address]), usdBefore);
    const callback = await s.send(market.guard, "uniswapV3SwapCallback", [1n, -1n, "0x"], { expect: "revert" });
    assert.equal(callback.status, "reverted");
    const wrongRecipient = await s.send(market.guard, "buyExactInput",
      [market.pool.address, 1_000_000n, 0n, market.pool.address, 0n],
      { account: buyer.account, expect: "revert" });
    assert.equal(wrongRecipient.status, "reverted");

    // The existing adaptive household uses guarded purchases and counts only
    // confirmed selected-NFT settlements. This exercises one isolated action,
    // not the unported household setup, savings basket or five-year world.
    const agent = new CreditHouseholdAgent({ account: buyer.account, settings: {
      surplus_policy: "adaptive", paydown_share: .1, paydown_double: .005, max_impact_bp: 9000, divest_rate: 0,
    } });
    let failSettlement = true, limitSlice = true;
    const failingOps = netCreditOps(s, world.net, async (owner, target, name, args) => {
      if (target.address === world.net.fund.address) {
        const receipt = await s.send(target, name, [args[0], args[1] + 1n], { account: owner, expect: "revert" });
        throw new TransactionReverted(name, "simulated mined refusal", receipt);
      }
      return s.send(target, name, args, { account: owner });
    });
    const limitedOps = { ...creditOps, retireHousehold: (owner, ids, target, expires, opts) =>
      (failSettlement ? failingOps : creditOps).retireHousehold(owner, ids,
        !failSettlement && limitSlice && target > 10_000_000n ? 10_000_000n : target, expires, opts) };
    const hworld = { accounting: "net", households: netHouseholdRepaymentOps(world, market,
      { facilityIds: () => [buyerId], creditOps: limitedOps }) };
    const [liquidity, slot, t0] = await Promise.all([s.call(market.pool, "liquidity"),
      s.call(market.pool, "slot0"), s.call(market.pool, "token0")]);
    const r0 = liquidity * Q96 / slot[0], r1 = liquidity * slot[0] / Q96;
    const [ru, rb] = (t0.toLowerCase() === market.usdc.address.toLowerCase() ? [r0, r1] : [r1, r0])
      .map((v) => Number(v) / 1e6);
    const state = { ru, rb, fee: .0005, basket_usd: 1.5 * ru / rb };
    const remaining = (await creditOps.facility(buyerId)).issued;
    const beforeHousehold = await s.call(market.usdc, "balanceOf", [buyer.account.address]);
    const refuseBuy = async (_world, _market, holder, amount, { minOut }) => {
      const receipt = await s.send(market.guard, "buyExactInput",
        [market.pool.address, amount, minOut, holder.address, 0n], { account: holder, expect: "revert" });
      throw new TransactionReverted("buyExactInput", "simulated price refusal", receipt);
    };
    const refuseWorld = { accounting: "net", households: netHouseholdRepaymentOps(world, market,
      { facilityIds: () => [buyerId], creditOps, buy: refuseBuy }) };
    await assert.rejects(agent.saveOrPayDown(refuseWorld, { ...state, basket_usd: 1.01 * ru / rb }, beforeHousehold),
      TransactionReverted);
    assert.equal(agent.netTradeUncertain, false);
    assert.equal(agent.netRepaymentReserve, null);
    assert.equal(await s.call(market.usdc, "balanceOf", [buyer.account.address]), beforeHousehold);
    assert.equal((await creditOps.facility(buyerId)).issued, remaining);
    await creditOps.authorizeReduction(buyer.account, buyerId, 1_000_000n, 0n,
      (await s.client.getBlock()).timestamp + 3600n);
    await agent.saveOrPayDown(hworld, state, beforeHousehold);
    assert.equal(agent.lastAction, "repay-pending");
    await agent.act(hworld, 0, 0);
    assert.equal(agent.lastAction, "repay-pending"); // untracked authorization blocks the whole turn
    assert.equal(await s.call(market.usdc, "balanceOf", [buyer.account.address]), beforeHousehold);
    assert.equal((await creditOps.facility(buyerId)).issued, remaining);
    await creditOps.cancelReduction(buyer.account, buyerId);
    await agent.saveOrPayDown(hworld, state, beforeHousehold);
    const cashAfterBuy = await s.call(market.usdc, "balanceOf", [buyer.account.address]);
    assert.ok(cashAfterBuy < beforeHousehold);
    assert.notEqual(agent.netRepaymentReserve, null);
    assert.equal(agent.lastAction, "repay-pending");
    assert.equal(agent.unwound, 0n);
    assert.ok(agent.netPendingReverted && agent.netPendingHash);
    await agent.act(hworld, 0, 0);
    assert.match(agent.lastError, /explicit cancellation/);
    assert.equal((await creditOps.facility(buyerId)).issued, remaining);
    assert.ok((await s.call(world.buck, "reductionAuthorization", [buyerId]))[0] > 0n);
    await agent.saveOrPayDown(hworld, state, cashAfterBuy);
    assert.equal(await s.call(market.usdc, "balanceOf", [buyer.account.address]), cashAfterBuy);
    const original = await s.call(world.buck, "reductionAuthorization", [buyerId]);
    agent.netPendingReverted = false;
    await assert.rejects(agent.cancelPendingRepayment(hworld), /outcome uncertain/);
    assert.equal(await agent.reconcilePendingRepayment(hworld), 0n);
    assert.equal(agent.netPendingReverted, true);
    await creditOps.authorizeReduction(buyer.account, buyerId, original[0], original[1] + 1n, original[2]);
    await assert.rejects(agent.cancelPendingRepayment(hworld), /changed/);
    await creditOps.authorizeReduction(buyer.account, buyerId, ...original);
    await agent.cancelPendingRepayment(hworld);
    failSettlement = false;
    const [laterLiquidity, laterSlot] = await Promise.all([s.call(market.pool, "liquidity"), s.call(market.pool, "slot0")]);
    const a0 = laterLiquidity * Q96 / laterSlot[0], a1 = laterLiquidity * laterSlot[0] / Q96;
    [state.ru, state.rb] = (t0.toLowerCase() === market.usdc.address.toLowerCase() ? [a0, a1] : [a1, a0])
      .map((v) => Number(v) / 1e6);
    await agent.saveOrPayDown(hworld, state, cashAfterBuy);
    const firstRetired = remaining - (await creditOps.facility(buyerId)).issued;
    assert.equal(firstRetired, 10_000_000n);
    assert.notEqual(agent.netRepaymentReserve, null);
    assert.equal(await s.call(market.usdc, "balanceOf", [buyer.account.address]), cashAfterBuy);
    limitSlice = false;
    await agent.saveOrPayDown(hworld, state, cashAfterBuy);
    const retired = remaining - (await creditOps.facility(buyerId)).issued;
    assert.ok(retired > firstRetired);
    assert.equal(retired, agent.unwound);
    assert.equal(await s.call(market.usdc, "balanceOf", [buyer.account.address]), cashAfterBuy);
    assert.equal((await s.call(world.credit, "issuanceOf", [market.providerCreditId]))[0], provider[0]);

    // Even if the transaction succeeds but its response is lost, this agent
    // must stop with an uncertain outcome instead of buying or paying twice.
    const lostId = await insureAsset(world, world.operator.account, buyer, { face: 200_000_000n, premiumRate: 0 });
    await creditOps.issue(buyer.account, lostId, 200_000_000n, 100_000_000n);
    await sellBuck(world, market, buyer, 99_000_000n);
    const receipts = [];
    const lostOps = netCreditOps(s, world.net, async (owner, target, name, args) => {
      const receipt = await s.send(target, name, args, { account: owner });
      if (target.address === world.net.fund.address) {
        receipts.push(receipt);
        throw new TransactionReceiptPending(receipt.transactionHash);
      }
      return receipt;
    });
    const lostLimited = { ...lostOps, retireHousehold: (owner, ids, target, expires, opts) =>
      lostOps.retireHousehold(owner, ids, target > 1_000_000n ? 1_000_000n : target, expires, opts) };
    const lostWorld = { accounting: "net", households: netHouseholdRepaymentOps(world, market,
      { facilityIds: () => [lostId], creditOps: lostLimited }) };
    const agent2 = new CreditHouseholdAgent({ account: buyer.account, settings: {
      surplus_policy: "adaptive", paydown_share: .1, paydown_double: .005, max_impact_bp: 9000, divest_rate: 0,
    } });
    const [newLiquidity, newSlot] = await Promise.all([s.call(market.pool, "liquidity"), s.call(market.pool, "slot0")]);
    const b0 = newLiquidity * Q96 / newSlot[0], b1 = newLiquidity * newSlot[0] / Q96;
    const [newRu, newRb] = (t0.toLowerCase() === market.usdc.address.toLowerCase() ? [b0, b1] : [b1, b0])
      .map((v) => Number(v) / 1e6);
    const state2 = { ru: newRu, rb: newRb, fee: .0005, basket_usd: 1.5 * newRu / newRb };
    const cash2 = await s.call(market.usdc, "balanceOf", [buyer.account.address]);
    await agent2.saveOrPayDown(lostWorld, state2, cash2);
    const cashAfterLost = await s.call(market.usdc, "balanceOf", [buyer.account.address]);
    assert.ok(receipts.length && receipts[0].status === "success");
    assert.equal((await creditOps.facility(lostId)).issued, 99_000_000n);
    assert.equal((await s.call(world.buck, "reductionAuthorization", [lostId]))[0], 0n);
    assert.equal(agent2.unwound, 0n);
    assert.ok(agent2.netPendingHash);
    assert.match(agent2.lastError, /outcome uncertain/);
    await agent2.saveOrPayDown(lostWorld, state2, cashAfterLost);
    assert.equal(await s.call(market.usdc, "balanceOf", [buyer.account.address]), cashAfterLost);
    await assert.rejects(agent2.cancelPendingRepayment(lostWorld), /reconcile/);
    assert.throws(() => lostOps.reductionFromReceipt(buyer.account, lostId, 1_000_000n, receipts[0],
      `0x${"11".repeat(32)}`), /hash mismatch/);
    const realHash = agent2.netPendingHash;
    agent2.netPendingHash = `0x${"11".repeat(32)}`;
    await agent2.act(lostWorld, 0, 0);
    assert.equal(agent2.lastAction, "repay-pending");
    assert.equal(agent2.unwound, 0n);
    assert.equal(await s.call(market.usdc, "balanceOf", [buyer.account.address]), cashAfterLost);
    agent2.netPendingHash = realHash;
    await agent2.act(lostWorld, 0, 0);
    assert.equal(agent2.lastAction, "repay-reconciled");
    assert.equal(agent2.unwound, 1_000_000n);
    assert.equal(await s.call(market.usdc, "balanceOf", [buyer.account.address]), cashAfterLost);
    await assert.rejects(agent2.reconcilePendingRepayment(lostWorld), /known transaction hash/);

    const lostBuyId = await insureAsset(world, world.operator.account, buyer, { face: 200_000_000n, premiumRate: 0 });
    await creditOps.issue(buyer.account, lostBuyId, 200_000_000n, 100_000_000n);
    await sellBuck(world, market, buyer, 99_000_000n);
    const purchases = [];
    const loseBuy = async (...args) => {
      purchases.push(await buyBuck(...args));
      throw new TransactionReceiptPending(purchases.at(-1).transactionHash);
    };
    const resumeLimited = { ...creditOps, retireHousehold: (owner, ids, target, expires, opts) =>
      creditOps.retireHousehold(owner, ids, target > 1_000_000n ? 1_000_000n : target, expires, opts) };
    const lostBuyWorld = { accounting: "net", households: netHouseholdRepaymentOps(world, market,
      { facilityIds: () => [lostBuyId], creditOps: resumeLimited, buy: loseBuy }) };
    const agent3 = new CreditHouseholdAgent({ account: buyer.account, settings: {
      surplus_policy: "adaptive", paydown_share: .1, paydown_double: .005, max_impact_bp: 9000, divest_rate: 0,
    } });
    const [buyLiquidity, buySlot] = await Promise.all([s.call(market.pool, "liquidity"), s.call(market.pool, "slot0")]);
    const c0 = buyLiquidity * Q96 / buySlot[0], c1 = buyLiquidity * buySlot[0] / Q96;
    const [buyRu, buyRb] = (t0.toLowerCase() === market.usdc.address.toLowerCase() ? [c0, c1] : [c1, c0])
      .map((v) => Number(v) / 1e6);
    const state3 = { ru: buyRu, rb: buyRb, fee: .0005, basket_usd: 1.5 * buyRu / buyRb };
    const cash3 = await s.call(market.usdc, "balanceOf", [buyer.account.address]);
    await assert.rejects(agent3.saveOrPayDown(lostBuyWorld, state3, cash3), TransactionReceiptPending);
    const cashAfterLostBuy = await s.call(market.usdc, "balanceOf", [buyer.account.address]);
    assert.ok(purchases.length && cashAfterLostBuy < cash3 && agent3.netTradeUncertain && agent3.netPendingBuyHash);
    assert.equal((await creditOps.facility(lostBuyId)).issued, 100_000_000n);
    await agent3.saveOrPayDown(lostBuyWorld, state3, cashAfterLostBuy);
    assert.match(agent3.lastError, /buyback outcome uncertain/);
    assert.equal(await s.call(market.usdc, "balanceOf", [buyer.account.address]), cashAfterLostBuy);
    const buyReceipt = await s.client.getTransactionReceipt({ hash: agent3.netPendingBuyHash });
    assert.throws(() => netPurchaseFromReceipt(world, market, buyer.account, buyReceipt, `0x${"11".repeat(32)}`),
      /hash mismatch/);
    const badWorld = { ...world, session: { send: async () => ({ ...buyReceipt, logs: [] }) } };
    await assert.rejects(buyBuck(badWorld, market, buyer, 1n), (err) => {
      assert.ok(err instanceof TransactionReceiptPending);
      assert.equal(err.transactionHash, agent3.netPendingBuyHash);
      return true;
    });
    await agent3.act(lostBuyWorld, 0, 0);
    assert.equal(agent3.lastAction, "buy-reconciled");
    assert.equal(agent3.netTradeUncertain, false);
    assert.notEqual(agent3.netRepaymentReserve, null);
    assert.equal(await s.call(market.usdc, "balanceOf", [buyer.account.address]), cashAfterLostBuy);
    await assert.rejects(agent3.reconcilePendingBuy(lostBuyWorld), /known transaction hash/);
    const [resumeLiquidity, resumeSlot] = await Promise.all([s.call(market.pool, "liquidity"), s.call(market.pool, "slot0")]);
    const d0 = resumeLiquidity * Q96 / resumeSlot[0], d1 = resumeLiquidity * resumeSlot[0] / Q96;
    [state3.ru, state3.rb] = (t0.toLowerCase() === market.usdc.address.toLowerCase() ? [d0, d1] : [d1, d0])
      .map((v) => Number(v) / 1e6);
    await agent3.saveOrPayDown(lostBuyWorld, state3, cashAfterLostBuy);
    assert.equal((await creditOps.facility(lostBuyId)).issued, 99_000_000n);
    assert.equal(await s.call(market.usdc, "balanceOf", [buyer.account.address]), cashAfterLostBuy);

    // A lost authorization response must never be mistaken for settlement.
    const authReceipts = [];
    const lostAuthorizationOps = netCreditOps(s, world.net, async (owner, target, name, args) => {
      const receipt = await s.send(target, name, args, { account: owner });
      if (name === "authorizeReduction" && args[1]) {
        authReceipts.push(receipt);
        throw new TransactionReceiptPending(receipt.transactionHash);
      }
      return receipt;
    });
    const authWorld = { accounting: "net", households: netHouseholdRepaymentOps(world, market,
      { facilityIds: () => [lostBuyId], creditOps: lostAuthorizationOps }) };
    const beforePrincipal = (await creditOps.facility(lostBuyId)).issued, beforeUnwound = agent3.unwound;
    assert.notEqual(agent3.netRepaymentReserve, null);
    assert.equal(await agent3.retireAcquired(authWorld, 1_000_000n, 0n), 0n);
    assert.equal(agent3.netPendingPhase, "authorization");
    assert.ok(agent3.netPendingHash);
    assert.equal((await creditOps.facility(lostBuyId)).issued, beforePrincipal);
    assert.equal(agent3.unwound, beforeUnwound);
    const auth = await s.call(world.buck, "reductionAuthorization", [lostBuyId]);
    assert.equal(auth[0], 1_000_000n);
    await assert.rejects(agent3.cancelPendingRepayment(authWorld), /outcome uncertain/);
    const authPlan = { tokenId: lostBuyId, principal: auth[0], minimumReliefScaled: auth[1] };
    assert.throws(() => creditOps.authorizationFromReceipt(buyer.account, authPlan, auth[2], authReceipts[0],
      `0x${"11".repeat(32)}`), /hash mismatch/);
    await creditOps.authorizeReduction(buyer.account, lostBuyId, auth[0], auth[1] + 1n, auth[2]);
    await assert.rejects(agent3.reconcilePendingRepayment(authWorld), /changed/);
    await creditOps.authorizeReduction(buyer.account, lostBuyId, ...auth);
    await agent3.act(authWorld, 0, 0);
    assert.equal(agent3.lastAction, "authorize-reconciled");
    assert.equal(agent3.netAuthorizationConfirmed, true);
    assert.equal(agent3.unwound, beforeUnwound);
    await agent3.cancelPendingRepayment(authWorld);
    assert.equal((await s.call(world.buck, "reductionAuthorization", [lostBuyId]))[0], 0n);
    assert.equal((await creditOps.facility(lostBuyId)).issued, beforePrincipal);

    // A confirmed authorization revert also leaves principal untouched; its
    // receipt must be reconciled before the household tries again.
    const refuseAuthorizationOps = netCreditOps(s, world.net, async (owner, target, name, args) => {
      if (name === "authorizeReduction" && args[1]) {
        const receipt = await s.send(target, name, [args[0], args[1], args[2], 0n], { account: owner, expect: "revert" });
        throw new TransactionReverted(name, "simulated expired authorization", receipt);
      }
      return s.send(target, name, args, { account: owner });
    });
    const refusedAuthWorld = { accounting: "net", households: netHouseholdRepaymentOps(world, market,
      { facilityIds: () => [lostBuyId], creditOps: refuseAuthorizationOps }) };
    assert.equal(await agent3.retireAcquired(refusedAuthWorld, 1_000_000n, 0n), 0n);
    assert.equal(agent3.netPendingPhase, "authorization");
    assert.equal(agent3.netPendingReverted, true);
    await assert.rejects(agent3.cancelPendingRepayment(refusedAuthWorld), /outcome uncertain/);
    await agent3.act(refusedAuthWorld, 0, 0);
    assert.equal(agent3.netPendingToken, null);
    assert.equal(agent3.lastAction, "authorize-reverted");
    assert.equal(agent3.unwound, beforeUnwound);
    assert.equal((await creditOps.facility(lostBuyId)).issued, beforePrincipal);

    const noHashOps = netCreditOps(s, world.net, async (owner, target, name, args) => {
      const receipt = await s.send(target, name, args, { account: owner });
      if (name === "authorizeReduction" && args[1]) throw new Error("transport lost both receipt and hash");
      return receipt;
    });
    const noHashWorld = { accounting: "net", households: netHouseholdRepaymentOps(world, market,
      { facilityIds: () => [lostBuyId], creditOps: noHashOps }) };
    assert.equal(await agent3.retireAcquired(noHashWorld, 1_000_000n, 0n), 0n);
    assert.equal(agent3.netPendingPhase, "authorization");
    assert.equal(agent3.netPendingHash, null);
    assert.equal(await agent3.pendingRepayment(noHashWorld), true);
    await agent3.act(noHashWorld, 0, 0);
    assert.equal(agent3.lastAction, "repay-pending");
    assert.equal(agent3.netPendingPhase, "authorization");
    await assert.rejects(agent3.reconcilePendingRepayment(noHashWorld), /known transaction hash/);
    await assert.rejects(agent3.cancelPendingRepayment(noHashWorld), /outcome uncertain/);
    assert.equal((await creditOps.facility(lostBuyId)).issued, beforePrincipal);
    await creditOps.cancelReduction(buyer.account, lostBuyId); // explicit external recovery in this fixture

    const malformedAuthOps = netCreditOps(s, world.net, async (owner, target, name, args) => {
      const receipt = await s.send(target, name, args, { account: owner });
      return name === "authorizeReduction" && args[1] ? {} : receipt;
    });
    const malformedBatch = await malformedAuthOps.retireHousehold(buyer.account, [lostBuyId], 1_000_000n,
      (await s.client.getBlock()).timestamp + 3600n);
    assert.equal(malformedBatch.status, "authorization-pending");
    assert.equal(malformedBatch.pendingPhase, "authorization");
    assert.equal(malformedBatch.pendingTransactionHash, null);
    assert.equal(malformedBatch.retired, 0n);
    assert.equal((await creditOps.facility(lostBuyId)).issued, beforePrincipal);
    await creditOps.cancelReduction(buyer.account, lostBuyId);

    // Preserve the first actual settlement if the second NFT's authorization
    // response is lost in the middle of a multi-facility batch.
    const batchIds = [];
    for (let n = 0; n < 2; n++) {
      const id = await insureAsset(world, world.operator.account, buyer, { face: 2_000_000n, premiumRate: 0 });
      await creditOps.issue(buyer.account, id, 2_000_000n, 1_000_000n);
      batchIds.push(id);
    }
    const batchOps = netCreditOps(s, world.net, async (owner, target, name, args) => {
      const receipt = await s.send(target, name, args, { account: owner });
      if (name === "authorizeReduction" && args[0] === batchIds[1]) {
        throw new TransactionReceiptPending(receipt.transactionHash);
      }
      return receipt;
    });
    let failNextBalance = false, capturedBatch;
    const badSession = Object.create(s);
    badSession.call = async (...args) => {
      if (failNextBalance && args[0].address === world.buck.address && args[1] === "balanceOf") {
        throw new Error("simulated post-settlement balance read failure");
      }
      return s.call(...args);
    };
    const capture = (ops) => ({ ...ops, retireHousehold: async (...args) => {
      capturedBatch = await ops.retireHousehold(...args);
      failNextBalance = true;
      return capturedBatch;
    } });
    const badBalanceWorld = (ids, ops) => ({ accounting: "net", households: netHouseholdRepaymentOps(
      { ...world, session: badSession }, market, { facilityIds: () => ids, creditOps: capture(ops) }) });
    const agent4 = new CreditHouseholdAgent({ account: buyer.account });
    agent4.netRepaymentReserve = 1n;
    assert.equal(await agent4.retireAcquired(badBalanceWorld(batchIds, batchOps), 2_000_000n, 0n), 1_000_000n);
    const batch = capturedBatch;
    assert.equal(batch.retired, 1_000_000n);
    assert.equal(batch.settlements.length, 1);
    assert.equal(batch.status, "authorization-pending");
    assert.equal(batch.pendingPhase, "authorization");
    assert.equal(batch.blockedTokenId, batchIds[1]);
    assert.ok(batch.pendingTransactionHash);
    assert.equal(agent4.netPendingHash, batch.pendingTransactionHash);
    assert.equal(agent4.netRepaymentReserve, 1n);
    assert.equal((await creditOps.facility(batchIds[0])).issued, 0n);
    assert.equal((await creditOps.facility(batchIds[1])).issued, 1_000_000n);
    await creditOps.cancelReduction(buyer.account, batchIds[1]);

    const newSmallFacility = async () => {
      const id = await insureAsset(world, world.operator.account, buyer, { face: 2_000_000n, premiumRate: 0 });
      await creditOps.issue(buyer.account, id, 2_000_000n, 1_000_000n);
      return id;
    };
    let first = await newSmallFacility(), second = await newSmallFacility();
    const planFailureOps = netCreditOps(s, world.net);
    const realPlan = planFailureOps.planReduction;
    planFailureOps.planReduction = async (...args) => {
      if (args[1] === second) throw new Error("simulated planning read failure");
      return realPlan(...args);
    };
    failNextBalance = false;
    const agent5 = new CreditHouseholdAgent({ account: buyer.account });
    agent5.netRepaymentReserve = 1n;
    assert.equal(await agent5.retireAcquired(badBalanceWorld([first, second], planFailureOps), 2_000_000n, 0n), 1_000_000n);
    assert.equal(capturedBatch.status, "plan-unavailable");
    assert.equal(capturedBatch.blockedTokenId, second);
    assert.equal(capturedBatch.retired, 1_000_000n);
    assert.equal(capturedBatch.settlements.length, 1);
    assert.match(capturedBatch.error, /planning read failure/);
    assert.match(agent5.lastError, /plan-unavailable/);
    assert.equal(agent5.netRepaymentReserve, 1n);
    const recheckWorld = badBalanceWorld([first, second], planFailureOps);
    assert.equal(await agent5.pendingRepayment(recheckWorld), true);
    await agent5.act(recheckWorld, 0, 0);
    assert.equal(agent5.lastAction, "repay-recheck");
    assert.equal(agent5.netRepaymentInterrupted, false);
    assert.equal((await creditOps.facility(first)).issued, 0n);
    assert.equal((await creditOps.facility(second)).issued, 1_000_000n);

    first = await newSmallFacility(); second = await newSmallFacility();
    const preflightFailureOps = netCreditOps(s, world.net), realSettle = preflightFailureOps.settlePlan;
    preflightFailureOps.settlePlan = async (owner, plan, deadline) => {
      if (plan.tokenId === second) throw new Error("simulated authorization preflight failure");
      return realSettle(owner, plan, deadline);
    };
    const failedPreflight = await preflightFailureOps.retireHousehold(buyer.account, [first, second], 2_000_000n,
      (await s.client.getBlock()).timestamp + 3600n);
    assert.equal(failedPreflight.status, "preflight-unavailable");
    assert.equal(failedPreflight.blockedTokenId, second);
    assert.equal(failedPreflight.retired, 1_000_000n);
    assert.equal(failedPreflight.settlements.length, 1);
    assert.equal((await creditOps.facility(first)).issued, 0n);
    assert.equal((await creditOps.facility(second)).issued, 1_000_000n);

    first = await newSmallFacility();
    failNextBalance = false;
    const agent6 = new CreditHouseholdAgent({ account: buyer.account });
    agent6.netRepaymentReserve = 1n;
    assert.equal(await agent6.retireAcquired(badBalanceWorld([first], creditOps), 2_000_000n, 0n), 1_000_000n);
    assert.equal(capturedBatch.status, "partial");
    assert.equal(capturedBatch.retired, 1_000_000n);
    assert.match(agent6.lastError, /balance unavailable/);
    assert.equal(agent6.netRepaymentReserve, 1n);
    assert.equal(await agent6.pendingRepayment(badBalanceWorld([first], creditOps)), true);
    assert.equal((await creditOps.facility(first)).issued, 0n);
  }
  // A nominal pool can remain nonzero when its full-value balance is zero.
  // Exact net output must refuse without consuming the buyer's settlement.
  await advanceTime(world, 9001 * (365 * DAY + 6 * 3600));
  assert.equal(await s.call(world.buck, "netBalanceOf", [lastMarket.pool.address]), 0n);
  assert.ok(await s.call(world.buck, "balanceOf", [lastMarket.pool.address]) > 0n);
  const cashBefore = await s.call(lastMarket.usdc, "balanceOf", [lastBuyer.account.address]);
  const empty = await s.send(lastMarket.guard, "buyExactOutput",
    [lastMarket.pool.address, 1_000_000n, 10_000_000n, lastBuyer.account.address, 0n],
    { account: lastBuyer.account, expect: "revert" });
  assert.equal(empty.status, "reverted");
  assert.equal(await s.call(lastMarket.usdc, "balanceOf", [lastBuyer.account.address]), cashBefore);
  assert.deepEqual(s.mismatches, []);
});
