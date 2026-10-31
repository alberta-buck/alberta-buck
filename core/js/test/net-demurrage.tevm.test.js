// A linked, cash-only BuckNet smoke run in the browser-compatible EVM.
// Test identities and static K deliberately do not model a real economy.
import { test } from "node:test";
import assert from "node:assert/strict";
import { encodeFunctionData } from "viem";
import { readFileSync } from "node:fs";
import { tevmSession } from "../src/backends.js";
import { loadArtifact } from "../src/nodefs.js";
import { boundActivatedCapacity, deployNetStack, netCreditOps, ReductionPending } from "../src/index.js";
import { advanceTime } from "../src/buckworld.js";

const RAW = 1_000_000n;
const YEAR = 365 * 86400 + 6 * 3600;
const V = JSON.parse(readFileSync(new URL("../../../test/fixtures/bucknet-credit-operations.json", import.meta.url)));
const required = ["BuckNet", "BuckNetMaturity", "BuckNetTransfers", "BuckNetQuoter", "JubileeFund", "IdentityRegistryHarness",
  "BuckCredit", "BuckKControllerStatic", "BuckKControllerDirect", "UniswapV3Factory", "BuckBasketEquity", "SimLP"];
let skip = false;
try {
  for (const name of required) loadArtifact(name);
  if (!loadArtifact("BuckNet").linkReferences) skip = "BuckNet link metadata unavailable; build the checkout";
} catch {
  skip = "net/simulation artifacts unavailable; run make nix-sim-build";
}

test("activated capacity respects shared integer limits", () => {
  const q = 10n ** 36n, maxBalance = 2n ** 79n - 1n, maxUint = 2n ** 256n - 1n;
  const held = (maxBalance - 10n) * q;
  assert.deepEqual(boundActivatedCapacity([4n, 4n], held, 5n * q, 0n), { potentialCapacity: 5n, sharedLimit: 5n });
  assert.deepEqual(boundActivatedCapacity([4n, 4n], held, 5n * q + 1n, 0n), { potentialCapacity: 4n, sharedLimit: 4n });
  assert.deepEqual(boundActivatedCapacity([4n, 4n], held, 0n, maxUint / q - 3n), { potentialCapacity: 3n, sharedLimit: 3n });
  assert.throws(() => boundActivatedCapacity([4n], maxBalance * q, 1n, 0n), /invalid/);
  assert.throws(() => boundActivatedCapacity([4n], 0n, 0n, maxUint / q + 1n), /invalid/);
});

test("linked BuckNet issues, accrues and retires cash-only credit", { skip }, async () => {
  const s = await tevmSession();
  const gov = s.account.address;
  const deploy = (name, args = []) => s.deploy(loadArtifact(name), args, { name });
  const registry = await deploy("IdentityRegistryHarness", [gov]);
  const credit = await deploy("BuckCredit");
  const controller = await deploy("BuckKControllerStatic", [10n ** 18n, gov]);
  const stack = await deployNetStack(s, loadArtifact, { registry, credit, controller, governance: gov });
  const { fund, token: buck } = stack;
  const factory = await deploy("UniswapV3Factory");
  const basket = await deploy("BuckBasketEquity",
    [buck.address, controller.address, factory.address, gov, 3000, 600, 64, 500n, 1000n]);
  const actor = await deploy("SimLP"); // Public local fixture; not an economic household.
  const g = { X: 1n, Y: 2n };
  const E = { R: g, C: g };
  for (const [target, carrying] of [[fund.address, true], [basket.address, false], [actor.address, false]]) {
    await s.send(registry, "bindContract", [target, g, E, true, carrying]);
  }
  await s.send(basket, "enableNetAccounting");
  await s.send(fund, "configure", [buck.address, basket.address]);
  const exec = (target, functionName, args) => s.send(actor, "exec", [target.address,
    encodeFunctionData({ abi: target.abi, functionName, args })]);
  await exec(credit, "setCreditIssuer", [gov, true]);
  await s.send(credit, "createCredit", [actor.address, 0, 100n * RAW, 100n * RAW, 0, 0, 0, 0]);
  await exec(buck, "issue", [0n, 100n * RAW, 100n * RAW]);
  const ops = netCreditOps(s, stack);
  const fresh = await ops.quoteReliefRate(0n, RAW);
  assert.equal(fresh.selectedSeconds, 0n);
  assert.equal(fresh.annualRateWad, null);
  assert.equal((await ops.quotePremiumRefund(0n, 0n)).reason, "no-premium");
  assert.equal(await s.call(buck, "balanceOf", [actor.address]), 100n * RAW);
  assert.equal(await s.call(buck, "totalIssued"), 100n * RAW);
  await advanceTime({ session: s }, YEAR);
  const [, cash] = await s.call(buck, "fundBalancesScaled");
  assert.equal(await s.call(buck, "balanceOf", [actor.address]), 98_019_867n);
  const [relief, quotedCash, exceeds] = await s.call(fund, "previewRelief", [0n, RAW]);
  assert.equal(quotedCash, cash);
  assert.equal(relief, cash / 100n);
  assert.equal(exceeds, false);
  const observed = await ops.quoteReliefRate(0n, RAW);
  assert.ok(Math.abs(Number(observed.annualRateWad) / 1e18 - (1 - Math.exp(-.02))) < 1e-8);
  assert.equal(observed.capped, false);
  assert.equal(observed.exceedsLiquidityLimit, false);
  const tooLarge = await ops.quoteReliefRate(0n, 100n * RAW);
  assert.equal(tooLarge.exceedsLiquidityLimit, true);
  assert.equal(tooLarge.capped, false);
  const deadline = (await s.client.getBlock()).timestamp + 3600n;
  await exec(buck, "authorizeReduction", [0n, RAW, 0n, deadline]);
  await exec(fund, "reduce", [0n, RAW]);
  assert.equal(await s.call(buck, "totalIssued"), 99n * RAW);
  assert.equal((await s.call(credit, "issuanceOf", [0n]))[0], 99n * RAW);
  assert.ok((await s.call(buck, "fundBalancesScaled"))[1] < cash);
  await advanceTime({ session: s }, 48 * YEAR);
  await s.send(credit, "createCredit", [actor.address, 0, 100n * RAW, 100n * RAW, 0, 0, 0, 0]);
  await exec(buck, "issue", [1n, 100n * RAW, 50n * RAW]);
  await exec(buck, "transfer", [fund.address, 40n * RAW]);
  const capped = await ops.quoteReliefRate(0n, RAW);
  assert.equal(capped.capped, true);
  assert.equal(capped.reliefScaled, RAW * 10n ** 36n);
  assert.notEqual(capped.annualRateWad, null);
  await advanceTime({ session: s }, 2 * YEAR);
  assert.equal(await s.call(buck, "maturitiesPending"), true);
  const capacity = await ops.quoteHouseholdActivatedCapacity(actor.address, [0n, 1n]);
  assert.equal(capacity.maturitiesPending, true);
  assert.ok(capacity.potentialCapacity > 0n);
  assert.equal(capacity.capacity, 0n);
  await assert.rejects(ops.quoteReliefRate(0n, RAW)); // stale denominator must not become a forecast
  assert.deepEqual(s.mismatches, []);
});

test("per-NFT operations honor the premium reserve and repay explicitly", { skip }, async () => {
  // The PID controller is real, but has no basket reference in this fixture:
  // this tests its neutral funding gate, not market-driven K or a household.
  const s = await tevmSession();
  const gov = s.account.address;
  const deploy = (name, args = []) => s.deploy(loadArtifact(name), args, { name });
  const registry = await deploy("IdentityRegistryHarness", [gov]);
  const credit = await deploy("BuckCredit");
  const controller = await deploy("BuckKControllerDirect", [100_000_000_000n, 643_004n, 0n,
    1800n, 0n, 950_000_000_000_000_000n, 750_000_000_000_000_000n, gov]);
  const stack = await deployNetStack(s, loadArtifact, { registry, credit, controller, governance: gov });
  const { fund, token: buck } = stack;
  const factory = await deploy("UniswapV3Factory");
  const basket = await deploy("BuckBasketEquity",
    [buck.address, controller.address, factory.address, gov, 3000, 600, 64, 500n, 1000n]);
  const insurer = await deploy("SimLP"), insurer2 = await deploy("SimLP");
  const provider = await deploy("SimLP"), debtor = await deploy("SimLP");
  const pool = await deploy("SimLP"), pool2 = await deploy("SimLP");
  const g = { X: 1n, Y: 2n }, E = { R: g, C: g };
  for (const [target, carrying] of [[fund.address, true], [basket.address, false], [insurer.address, false],
    [insurer2.address, false], [provider.address, false], [debtor.address, false]]) {
    await s.send(registry, "bindContract", [target, g, E, true, carrying]);
  }
  const exec = (actor, target, functionName, args, expect = "ok") => s.send(actor, "exec", [target.address,
    encodeFunctionData({ abi: target.abi, functionName, args })], { expect });
  await exec(insurer, registry, "bindContract", [pool.address, g, E, true, true]);
  await exec(insurer2, registry, "bindContract", [pool2.address, g, E, true, true]);
  await exec(insurer, buck, "setInsurerPool", [pool.address]);
  await exec(insurer2, buck, "setInsurerPool", [pool2.address]);
  await exec(pool, buck, "approve", [buck.address, (1n << 256n) - 1n]);
  await exec(pool2, buck, "approve", [buck.address, (1n << 256n) - 1n]);
  await s.send(basket, "enableNetAccounting");
  await s.send(fund, "configure", [buck.address, basket.address]);

  const actors = new Map([provider, debtor].map((actor) => [actor.address.toLowerCase(), actor]));
  const ops = netCreditOps(s, stack, (owner, target, method, args) =>
    exec(actors.get(owner.toLowerCase()), target, method, args));
  const face = BigInt(V.face), premium = BigInt(V.premium), reserve = BigInt(V.reserve);
  const spendable = BigInt(V.requested_spendable), issued = BigInt(V.issued), reduction = BigInt(V.reduction);
  for (const [actor, rate] of [[provider, 0], [debtor, V.premium_rate_bp]]) {
    await exec(actor, credit, "setCreditIssuer", [insurer.address, true]);
    await exec(insurer, credit, "createCredit", [actor.address, 0, face, face, 0, 0, 0, rate]);
  }
  const providerId = await s.call(credit, "tokenOfOwnerByIndex", [provider.address, 0n]);
  const debtorId = await s.call(credit, "tokenOfOwnerByIndex", [debtor.address, 0n]);
  await ops.issue(provider.address, providerId, face, BigInt(V.provider_issuance));
  const quote = await ops.quoteIssue(debtorId, face);
  assert.deepEqual([quote.premium, quote.reserveRequired, quote.maxSpendable, quote.reserveShortfall],
    [premium, reserve, spendable, reserve]);
  const refused = await exec(debtor, buck, "issue", [debtorId, face, spendable], "revert");
  assert.equal(refused.status, "reverted");
  assert.equal((await ops.facility(debtorId)).issued, 0n);

  // Exact reserve at one block can fall short as early as the next second.
  await exec(provider, buck, "transfer", [debtor.address, reserve]);
  assert.equal((await ops.quoteIssue(debtorId, face)).reserveShortfall, 0n);
  await advanceTime({ session: s }, 1);
  assert.ok((await ops.quoteIssue(debtorId, face)).reserveShortfall > 0n);
  const decayed = await exec(debtor, buck, "issue", [debtorId, face, spendable], "revert");
  assert.equal(decayed.status, "reverted");
  await exec(provider, buck, "transfer", [debtor.address, BigInt(V.reserve_buffer)]);
  assert.equal((await ops.quoteIssue(debtorId, face)).reserveShortfall, 0n);
  await ops.issue(debtor.address, debtorId, face, spendable);
  const state = await ops.facility(debtorId);
  assert.deepEqual([state.face, state.activated, state.issued, state.premiumNominal],
    [face, face, issued, premium]);
  assert.equal(state.insurerPool.toLowerCase(), pool.address.toLowerCase());
  assert.equal(await s.call(buck, "balanceOf", [pool.address]), premium);
  assert.equal(await s.call(buck, "balanceOf", [pool2.address]), 0n);

  // The same household's second facility remains with its own insurer.
  await exec(debtor, credit, "setCreditIssuer", [insurer2.address, true]);
  await exec(insurer2, credit, "createCredit", [debtor.address, 0, face, face, 0, 0, 0, V.second_premium_rate_bp]);
  const secondId = await s.call(credit, "tokenOfOwnerByIndex", [debtor.address, 1n]);
  const secondQuote = await ops.quoteIssue(secondId, face);
  assert.equal(secondQuote.premium, BigInt(V.second_premium));
  assert.equal(secondQuote.reserveShortfall, 0n);
  await ops.issue(debtor.address, secondId, face, BigInt(V.second_spendable));
  const second = await ops.facility(secondId);
  assert.equal(second.issued, BigInt(V.second_issued));
  assert.equal(second.insurerPool.toLowerCase(), pool2.address.toLowerCase());
  assert.equal(await s.call(buck, "balanceOf", [pool2.address]), BigInt(V.second_premium));

  await exec(provider, buck, "transfer", [debtor.address, reduction]);
  assert.equal((await ops.facility(debtorId)).issued, issued);
  assert.equal((await ops.facility(secondId)).issued, BigInt(V.second_issued));
  await advanceTime({ session: s }, YEAR);
  const household = await ops.household(debtor.address, [debtorId, secondId]);
  assert.equal(household.issued, issued + BigInt(V.second_issued));
  assert.equal(household.premiumNominal, premium + BigInt(V.second_premium));
  assert.equal(household.spendable, await s.call(buck, "balanceOf", [debtor.address]));
  await assert.rejects(ops.household(debtor.address, [debtorId, debtorId]), /duplicate/);
  await assert.rejects(ops.household(debtor.address, [providerId]), /foreign/);
  await assert.rejects(ops.household(pool.address, []), /non-Carrying/);
  const capped = await ops.planReduction(debtor.address, debtorId, issued);
  assert.ok(capped.reason === "ready" && capped.principal > 0n && capped.principal < issued);
  const affordable = await ops.planReduction(debtor.address, debtorId, reduction, { buffer: household.spendable - 1000n });
  assert.ok(affordable.reason === "ready" && affordable.principal > 0n && affordable.principal < reduction);
  assert.ok(affordable.principal * 10n ** 36n - affordable.minimumReliefScaled <= 1000n * 10n ** 36n);
  const empty = await ops.planReduction(debtor.address, debtorId, reduction, { buffer: household.spendable + 1n });
  assert.equal(empty.reason, "owner-funds");
  assert.equal(empty.principal, 0n);
  await assert.rejects(ops.planReduction(debtor.address, debtorId, reduction, { reliefSlippageBp: 1.5 }), /invalid/);
  assert.equal((await ops.facility(debtorId)).issued, issued);
  assert.ok(await s.call(buck, "netBalanceOf", [pool.address]) < premium);
  const [relief, , exceeds] = await ops.quoteReduction(debtorId, reduction);
  assert.ok(relief > 0n && !exceeds);
  const deadline = (await s.client.getBlock()).timestamp + 3600n;
  const plan = await ops.planReduction(debtor.address, debtorId, reduction);
  assert.equal(plan.reason, "ready");
  assert.equal(plan.principal, reduction);
  await ops.authorizeReduction(debtor.address, debtorId, reduction, 0n, deadline);
  assert.equal((await ops.planReduction(debtor.address, debtorId, reduction)).reason, "authorization-pending");
  await ops.cancelReduction(debtor.address, debtorId);
  const failing = netCreditOps(s, stack, (owner, target, method, args) => {
    if (target.address === fund.address) throw new Error("simulated settlement transport failure");
    return exec(actors.get(owner.toLowerCase()), target, method, args);
  });
  await assert.rejects(failing.settlePlan(debtor.address, plan, deadline), ReductionPending);
  assert.equal((await s.call(buck, "reductionAuthorization", [debtorId]))[0], reduction);
  assert.equal((await ops.facility(debtorId)).issued, issued);
  await ops.cancelReduction(debtor.address, debtorId);
  const pending = await failing.retireHousehold(debtor.address, [debtorId, secondId], reduction, deadline);
  assert.equal(pending.status, "authorization-pending");
  assert.equal(pending.blockedTokenId, debtorId);
  assert.equal(pending.retired, 0n);
  assert.equal(pending.pendingPrincipal, reduction);
  assert.equal(pending.pendingDeadline, deadline);
  assert.equal(pending.pendingTransactionHash, null);
  assert.equal(pending.pendingReverted, false);
  assert.equal((await s.call(buck, "reductionAuthorization", [debtorId]))[0], reduction);
  await ops.reduceAuthorized(debtor.address, debtorId, reduction);
  const after = await ops.facility(debtorId);
  assert.equal(after.issued, issued - reduction);
  assert.equal(after.premiumNominal, premium - BigInt(V.nominal_refund));
  assert.equal(await s.call(buck, "balanceOf", [pool.address]), premium - BigInt(V.nominal_refund));
  assert.equal((await ops.facility(secondId)).issued, BigInt(V.second_issued));
  assert.equal(await s.call(buck, "balanceOf", [pool2.address]), BigInt(V.second_premium));
  const first = await ops.retireHousehold(debtor.address, [secondId], 9n * RAW, deadline);
  assert.equal(first.status, "complete");
  assert.equal(first.retired, 9n * RAW);
  const batch = await ops.retireHousehold(debtor.address, [secondId, debtorId], 2n * RAW, deadline);
  assert.equal(batch.status, "complete");
  assert.equal(batch.retired, 2n * RAW);
  assert.equal(batch.settlements.length, 2);
  assert.equal((await ops.facility(secondId)).issued, 0n);
  assert.equal((await ops.facility(debtorId)).issued, issued - 2n * RAW);
  assert.equal(batch.reliefScaled + batch.ownerCostScaled, batch.retired * 10n ** 36n);

  const beforeRate = await ops.quoteReliefRate(debtorId, RAW);
  await exec(provider, buck, "transfer", [fund.address, 2n * RAW]);
  const afterRate = await ops.quoteReliefRate(debtorId, RAW);
  assert.ok(afterRate.annualRateWad > beforeRate.annualRateWad);
  await exec(insurer, credit, "createCredit", [debtor.address, 0, face, face, 0, 0, 0, 50]);
  const thirdId = await s.call(credit, "tokenOfOwnerByIndex", [debtor.address, 2n]);
  const refund = await ops.quotePremiumRefund(thirdId, face);
  assert.equal(refund.reason, "cash-only");
  assert.equal(refund.pool.toLowerCase(), pool.address.toLowerCase());
  assert.ok(refund.premium === 5n * RAW && refund.refundFractionWad > 0n && refund.refundFractionWad < 10n ** 18n);
  await ops.issue(debtor.address, thirdId, face, RAW);
  const [gross, net] = await s.call(buck, "accountBalancesScaled", [pool.address]);
  const delta = net * 10n ** 18n / gross - refund.refundFractionWad;
  assert.ok((delta < 0n ? -delta : delta) < 10n ** 10n);
  // Activated but unissued facility room mints new BUCK without buying
  // additional coverage or paying the insurer premium a second time.
  const thirdBefore = await ops.facility(thirdId), existing = await ops.quoteIssue(thirdId, 0n);
  assert.equal(existing.premium, 0n);
  assert.equal(existing.reserveRequired, 0n);
  assert.ok(existing.maxSpendable >= RAW);
  const householdCapacity = await ops.quoteHouseholdActivatedCapacity(debtor.address, [debtorId, thirdId]);
  assert.equal(householdCapacity.maturitiesPending, false);
  assert.equal(householdCapacity.blockNumber, (await s.client.getBlock()).number);
  assert.deepEqual(householdCapacity.facilities, [
    { tokenId: debtorId, maxSpendable: (await ops.quoteIssue(debtorId, 0n)).maxSpendable },
    { tokenId: thirdId, maxSpendable: existing.maxSpendable },
  ]);
  const independentSum = householdCapacity.facilities.reduce((total, x) => total + x.maxSpendable, 0n);
  assert.equal(householdCapacity.capacity, independentSum < householdCapacity.sharedLimit ? independentSum : householdCapacity.sharedLimit);
  await assert.rejects(ops.quoteHouseholdActivatedCapacity(debtor.address, [thirdId, thirdId]), /duplicate/);
  await assert.rejects(ops.quoteHouseholdActivatedCapacity(debtor.address, [providerId]), /foreign/);
  await ops.issue(debtor.address, thirdId, 0n, RAW);
  const thirdAfter = await ops.facility(thirdId);
  assert.equal(thirdAfter.activated, thirdBefore.activated);
  assert.equal(thirdAfter.premiumNominal, thirdBefore.premiumNominal);
  assert.equal(thirdAfter.issued, thirdBefore.issued + RAW);
  await exec(debtor, credit, "setCreditIssuer", [provider.address, true]);
  await exec(provider, credit, "createCredit", [debtor.address, 0, face, face, 0, 0, 0, 50]);
  const fourthId = await s.call(credit, "tokenOfOwnerByIndex", [debtor.address, 3n]);
  const missing = await ops.quotePremiumRefund(fourthId, face);
  assert.equal(missing.reason, "no-insurer-pool");
  assert.equal(missing.refundFractionWad, null);
  assert.deepEqual(s.mismatches, []);
});
