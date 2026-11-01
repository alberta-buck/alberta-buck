// A linked, cash-only BuckNet smoke run in the browser-compatible EVM.
// Test identities and static K deliberately do not model a real economy.
import { test } from "node:test";
import assert from "node:assert/strict";
import { encodeFunctionData } from "viem";
import { readFileSync } from "node:fs";
import { tevmSession } from "../src/backends.js";
import { loadArtifact } from "../src/nodefs.js";
import { boundActivatedCapacity, deployNetStack, issueCoverageCandidates, IssuancePending, netCreditOps, ReductionPending } from "../src/index.js";
import { TransactionReceiptPending } from "../src/session.js";
import { advanceTime } from "../src/buckworld.js";
import { bestQuotedRefinance, refinance, refinanceQuoted } from "../src/agents/household-policy.js";

const RAW = 1_000_000n;
const ZERO = "0x0000000000000000000000000000000000000000";
const YEAR = 365 * 86400 + 6 * 3600;
const V = JSON.parse(readFileSync(new URL("../../../test/fixtures/bucknet-credit-operations.json", import.meta.url)));
const required = ["BuckNet", "BuckNetMaturity", "BuckNetTokenOps", "BuckNetQuoter", "JubileeFund", "IdentityRegistryHarness",
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

test("coverage candidates are bounded and include repair estimates", () => {
  assert.deepEqual(issueCoverageCandidates(100n, 40n, 30n, 14n, 75n * 10n ** 16n, 100n, 1n),
    [0n, 1n, 7n, 15n, 22n, 26n, 30n, 34n, 37n, 38n, 42n, 45n, 52n, 60n]);
  assert.deepEqual(issueCoverageCandidates(100n, 40n, 0n, 14n, 10n ** 18n, 100n, 1n), [0n]);
  const slow = issueCoverageCandidates(100n, 40n, 30n, 14n, 10n ** 17n, 100n, 1n);
  assert.ok(slow.length <= 15 && slow[0] === 0n && slow.at(-1) === 60n);
  for (const bad of [[100n, 101n, 30n, 14n, 10n ** 18n, 100n, 1n],
    [100n, 40n, 101n, 14n, 10n ** 18n, 100n, 1n],
    [100n, 40n, 30n, 14n, 10n ** 18n, 1000n, 1n], [100n, 40n, 30n, 14n, 10n ** 18n, 100n, 0n]]) {
    assert.throws(() => issueCoverageCandidates(...bad), /coverage/);
  }
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
  await exec(buck, "issue", [0n, 100n * RAW, 100n * RAW, 0n, ZERO]);
  const issuedAt = (await s.client.getBlock()).timestamp;
  const ops = netCreditOps(s, stack);
  const fresh = await ops.quoteReliefRate(0n, RAW);
  assert.equal(fresh.selectedSeconds, 0n);
  assert.equal(fresh.annualRateWad, null);
  assert.equal((await ops.quotePremiumRefund(0n, 0n)).reason, "no-premium");
  assert.equal(await s.call(buck, "balanceOf", [actor.address]), 100n * RAW);
  assert.equal(await s.call(buck, "totalIssued"), 100n * RAW);
  await s.send(controller, "setBuckK", [5n * 10n ** 17n]);
  const shocked = await ops.assessIssueCandidate(actor.address, 0n, 0n, RAW, 0n);
  assert.equal(shocked.currentK, 5n * 10n ** 17n);
  assert.equal(shocked.shortfallBefore, 50n * RAW);
  assert.ok(shocked.reasons.includes("insufficient-capacity"));
  await s.send(controller, "setBuckK", [10n ** 18n]);
  await advanceTime({ session: s }, YEAR - Number((await s.client.getBlock()).timestamp - issuedAt));
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
  const pending = await ops.assessIssueCandidate(actor.address, 0n, 0n, RAW, 0n);
  assert.ok(pending.reasons.includes("authorization-pending"));
  await assert.rejects(ops.issueCandidate(actor.address, { ...pending, reasons: [] }), /authorization pending/);
  await exec(fund, "reduce", [0n, RAW]);
  assert.equal(await s.call(buck, "totalIssued"), 99n * RAW);
  assert.equal((await s.call(credit, "issuanceOf", [0n]))[0], 99n * RAW);
  assert.ok((await s.call(buck, "fundBalancesScaled"))[1] < cash);
  await advanceTime({ session: s }, 48 * YEAR);
  await s.send(credit, "createCredit", [actor.address, 0, 100n * RAW, 100n * RAW, 0, 0, 0, 0]);
  await exec(buck, "issue", [1n, 100n * RAW, 50n * RAW, 0n, ZERO]);
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
  assert.ok((await ops.assessIssueCandidate(actor.address, 1n, 0n, RAW, 0n)).reasons.includes("maturities-pending"));
  assert.equal((await ops.issueCoverageMenu(actor.address, 1n, RAW, 0n)).length, 1);
  await assert.rejects(ops.quoteReliefRate(0n, RAW)); // stale denominator must not become a forecast
  await s.send(buck, "processMaturities", [10n]);
  assert.equal(await s.call(buck, "maturitiesPending"), false);
  assert.ok(await s.call(credit, "depreciatedFaceValue", [0n]) > await s.call(credit, "currentValue", [0n]));
  const matured = await ops.quoteHouseholdActivatedCapacity(actor.address, [0n]);
  assert.equal(matured.capacity, 0n);
  assert.equal(matured.unactivatedPresent, 0n); // a matured NFT cannot issue again
  assert.ok((await ops.assessIssueCandidate(actor.address, 0n, 0n, RAW, 0n)).reasons.includes("facility-matured"));
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
  // Preserve the hash when the dispatch response disappears after mining;
  // authenticate the event before any future action can count the issuance.
  const providerPlan = await ops.assessIssueCandidate(provider.address, providerId, face, BigInt(V.provider_issuance), 0n);
  assert.deepEqual(providerPlan.reasons, []);
  const unknownOps = netCreditOps(s, stack, async () => { throw new Error("dispatch outcome unavailable"); });
  await assert.rejects(unknownOps.issueCandidate(provider.address, providerPlan),
    (err) => err instanceof IssuancePending && err.transactionHash === null && !err.reverted);
  assert.equal((await ops.facility(providerId)).issued, 0n);
  const lostOps = netCreditOps(s, stack, async (owner, target, method, args) => {
    const receipt = await exec(actors.get(owner.toLowerCase()), target, method, args);
    throw new TransactionReceiptPending(receipt.transactionHash);
  });
  let interrupted;
  await assert.rejects(lostOps.issueCandidate(provider.address, providerPlan), (err) => {
    interrupted = err;
    return err instanceof IssuancePending && Boolean(err.transactionHash) && !err.reverted;
  });
  const providerReceipt = await s.client.getTransactionReceipt({ hash: interrupted.transactionHash });
  const evidence = ops.issuanceFromReceipt(provider.address, providerPlan, providerReceipt, interrupted.transactionHash);
  assert.deepEqual([evidence.principal, evidence.premium, evidence.spendable, evidence.pool.toLowerCase()],
    [BigInt(V.provider_issuance), 0n, BigInt(V.provider_issuance), ZERO]);
  assert.throws(() => ops.issuanceFromReceipt(provider.address, providerPlan, providerReceipt, `0x${"00".repeat(32)}`), /hash/);
  assert.throws(() => ops.issuanceFromReceipt(provider.address, { ...providerPlan, coverage: face - 1n },
    providerReceipt, interrupted.transactionHash), /candidate/);
  const duplicated = { ...providerReceipt, logs: [...providerReceipt.logs, ...providerReceipt.logs] };
  assert.throws(() => ops.issuanceFromReceipt(provider.address, providerPlan, duplicated, interrupted.transactionHash), /ambiguous/);
  assert.equal((await ops.facility(providerId)).issued, BigInt(V.provider_issuance));
  const quote = await ops.quoteIssue(debtorId, face);
  assert.deepEqual([quote.premium, quote.reserveRequired, quote.maxSpendable, quote.reserveShortfall],
    [premium, reserve, spendable, reserve]);
  const candidate = await ops.assessIssueCandidate(debtor.address, debtorId, face, spendable, premium,
    { reserveBuffer: BigInt(V.reserve_buffer) });
  assert.equal(candidate.blockNumber, (await s.client.getBlock()).number);
  assert.equal(candidate.premiumPool.toLowerCase(), pool.address.toLowerCase());
  assert.deepEqual([candidate.premium, candidate.reserveRequired, candidate.maxSpendable], [premium, reserve, spendable]);
  assert.equal(candidate.reserveShortfall, reserve + BigInt(V.reserve_buffer));
  assert.deepEqual(candidate.reasons, ["reserve-shortfall"]);
  await assert.rejects(ops.issueCandidate(debtor.address, candidate), /not available/);
  assert.deepEqual(await ops.assessIssueCandidate(debtor.address, debtorId, face, spendable, premium,
    { reserveBuffer: BigInt(V.reserve_buffer), blockNumber: candidate.blockNumber }), candidate);
  assert.ok((await ops.assessIssueCandidate(debtor.address, debtorId, face, spendable, premium - 1n)).reasons.includes("premium-limit"));
  await assert.rejects(ops.assessIssueCandidate(debtor.address, providerId, face, spendable, 0n), /foreign/);
  await assert.rejects(ops.assessIssueCandidate(debtor.address, debtorId, face + 1n, spendable, premium), /unactivated/);
  await assert.rejects(ops.assessIssueCandidate(debtor.address, debtorId, face, 0n, premium), /candidate/);
  await assert.rejects(ops.assessIssueCandidate(debtor.address, debtorId, face, spendable, premium,
    { blockNumber: -1n }), /candidate/);
  assert.throws(() => ops.issue(debtor.address, debtorId, face, spendable), /maximum premium/);
  assert.throws(() => ops.issue(debtor.address, debtorId, face, spendable, -1n, pool.address), /maximum premium/);
  assert.throws(() => ops.issue(debtor.address, debtorId, face, spendable, premium), /insurer pool/);
  assert.throws(() => ops.issue(debtor.address, debtorId, face, spendable, premium, "invalid"), /insurer pool/);
  const refused = await exec(debtor, buck, "issue", [debtorId, face, spendable, premium, pool.address], "revert");
  assert.equal(refused.status, "reverted");
  assert.equal((await ops.facility(debtorId)).issued, 0n);

  // Exact reserve at one block can fall short as early as the next second.
  await exec(provider, buck, "transfer", [debtor.address, reserve]);
  assert.equal((await ops.quoteIssue(debtorId, face)).reserveShortfall, 0n);
  await advanceTime({ session: s }, 1);
  assert.ok((await ops.quoteIssue(debtorId, face)).reserveShortfall > 0n);
  const decayed = await exec(debtor, buck, "issue", [debtorId, face, spendable, premium, pool.address], "revert");
  assert.equal(decayed.status, "reverted");
  await exec(provider, buck, "transfer", [debtor.address, BigInt(V.reserve_buffer)]);
  assert.equal((await ops.quoteIssue(debtorId, face)).reserveShortfall, 0n);
  const supplyBefore = await s.call(buck, "totalIssued"), balanceBefore = await s.call(buck, "balanceOf", [debtor.address]);
  await exec(insurer, credit, "updateCredit", [debtorId, face, face, 0, 0, 0, V.premium_rate_bp + 1]);
  assert.ok((await ops.quoteIssue(debtorId, face)).premium > premium);
  const stale = await exec(debtor, buck, "issue", [debtorId, face, spendable, premium, pool.address], "revert");
  assert.equal(stale.status, "reverted");
  assert.equal(await s.call(buck, "totalIssued"), supplyBefore);
  const balanceAfter = await s.call(buck, "balanceOf", [debtor.address]);
  assert.ok(balanceBefore >= balanceAfter && balanceBefore - balanceAfter <= 10n);
  assert.equal((await ops.facility(debtorId)).activated, 0n);
  assert.equal((await ops.facility(debtorId)).issued, 0n);
  assert.equal(await s.call(buck, "balanceOf", [pool.address]), 0n);
  await exec(insurer, credit, "updateCredit", [debtorId, face, face, 0, 0, 0, V.premium_rate_bp]);
  let debtorPlan = await ops.assessIssueCandidate(debtor.address, debtorId, face, spendable, premium);
  assert.deepEqual(debtorPlan.reasons, []);
  const replacement = await deploy("SimLP");
  await exec(insurer, registry, "bindContract", [replacement.address, g, E, true, true]);
  await exec(insurer, buck, "setInsurerPool", [replacement.address]);
  const eitherOps = netCreditOps(s, stack, (owner, target, method, args) =>
    exec(actors.get(owner.toLowerCase()), target, method, args, "revert"));
  await assert.rejects(eitherOps.issueCandidate(debtor.address, debtorPlan),
    (err) => err instanceof IssuancePending && Boolean(err.transactionHash) && err.reverted);
  assert.equal(await s.call(buck, "totalIssued"), supplyBefore);
  assert.equal((await ops.facility(debtorId)).activated, 0n);
  assert.equal((await ops.facility(debtorId)).issued, 0n);
  assert.equal(await s.call(buck, "balanceOf", [replacement.address]), 0n);
  await exec(insurer, buck, "setInsurerPool", [pool.address]);
  debtorPlan = await ops.assessIssueCandidate(debtor.address, debtorId, face, spendable, premium);
  const issuedEvent = await ops.issueCandidate(debtor.address, debtorPlan);
  assert.deepEqual([issuedEvent.principal, issuedEvent.premium, issuedEvent.pool.toLowerCase()],
    [issued, premium, pool.address.toLowerCase()]);
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
  await ops.issue(debtor.address, secondId, face, BigInt(V.second_spendable), secondQuote.premium, pool2.address);
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
  const thirdPlan = await ops.assessIssueCandidate(debtor.address, thirdId, face, RAW, refund.premium);
  const omitEvents = netCreditOps(s, stack, async (owner, target, method, args) => {
    const receipt = await exec(actors.get(owner.toLowerCase()), target, method, args);
    return { ...receipt, logs: [] };
  });
  let omitted;
  await assert.rejects(omitEvents.issueCandidate(debtor.address, thirdPlan), (err) => {
    omitted = err;
    return err instanceof IssuancePending && Boolean(err.transactionHash) && !err.reverted;
  });
  const actual = await s.client.getTransactionReceipt({ hash: omitted.transactionHash });
  const verified = ops.issuanceFromReceipt(debtor.address, thirdPlan, actual, omitted.transactionHash);
  assert.equal(verified.premium, refund.premium);
  assert.equal(verified.principal, RAW + refund.premium);
  const [gross, net] = await s.call(buck, "accountBalancesScaled", [pool.address]);
  const delta = net * 10n ** 18n / gross - refund.refundFractionWad;
  assert.ok((delta < 0n ? -delta : delta) < 10n ** 10n);
  // Activated but unissued facility room mints new BUCK without buying
  // additional coverage or paying the insurer premium a second time.
  const thirdBefore = await ops.facility(thirdId), existing = await ops.quoteIssue(thirdId, 0n);
  assert.equal(existing.premium, 0n);
  assert.equal(existing.reserveRequired, 0n);
  assert.ok(existing.maxSpendable >= RAW);
  const activatedMenu = await ops.issueCoverageMenu(debtor.address, thirdId, RAW, 0n);
  assert.equal(activatedMenu.length, 1);
  assert.deepEqual(activatedMenu[0].reasons, []);
  assert.equal(activatedMenu[0].coverage, 0n);
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
  await ops.issue(debtor.address, thirdId, 0n, RAW, 0n, ZERO);
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
  assert.ok((await ops.assessIssueCandidate(debtor.address, fourthId, face, RAW, face)).reasons.includes("insurer-pool-unavailable"));
  // The unactivated share follows the asset's actual present mark.
  const v = V.depreciating_candidate;
  const start = (await s.client.getBlock()).timestamp;
  await exec(insurer, credit, "createCredit", [debtor.address, 0, BigInt(v.face), 0n, 1, v.depreciation_bp,
    start, v.premium_rate_bp]);
  const depreciatingId = await s.call(credit, "tokenOfOwnerByIndex", [debtor.address, 4n]);
  await ops.issue(debtor.address, depreciatingId, BigInt(v.initial_coverage), BigInt(v.initial_spendable),
    BigInt(v.initial_premium_limit), pool.address);
  await advanceTime({ session: s }, v.comparison_year * YEAR);
  const marked = await s.call(credit, "depreciatedFaceValue", [depreciatingId]);
  const active = await s.call(credit, "currentValue", [depreciatingId]);
  const valued = await ops.quoteHouseholdActivatedCapacity(debtor.address, [depreciatingId]);
  assert.equal(valued.unactivatedPresent, marked - active);
  assert.ok(valued.unactivatedPresent <= 48n * RAW && valued.unactivatedPresent > 48n * RAW - 10n);

  // The facility becomes over-issued as its asset depreciates. Activating a
  // small amount cannot repair it, but its remaining coverage can.
  await advanceTime({ session: s }, (v.repair_year - v.comparison_year) * YEAR);
  const requested = BigInt(v.requested_spendable), expectedShortfall = BigInt(v.shortfall_before);
  const blocked = await ops.assessIssueCandidate(debtor.address, depreciatingId, 0n, requested, 0n,
    { reserveBuffer: BigInt(V.reserve_buffer) });
  assert.equal(blocked.premiumPool, ZERO);
  assert.ok(blocked.shortfallBefore >= expectedShortfall && blocked.shortfallBefore < expectedShortfall + 10n);
  assert.equal(blocked.reserveShortfall, 0n); // no premium, no buffer gate
  assert.ok(blocked.reasons.includes("insufficient-capacity"));
  const partial = await ops.assessIssueCandidate(debtor.address, depreciatingId, BigInt(v.partial_coverage), requested, RAW);
  assert.ok(partial.shortfallAfterPremium > 0n && partial.reasons.includes("insufficient-capacity"));
  const repaired = await ops.assessIssueCandidate(debtor.address, depreciatingId, BigInt(v.remaining_coverage), requested, 2n * RAW,
    { reserveBuffer: BigInt(V.reserve_buffer) });
  assert.equal(repaired.blockNumber, (await s.client.getBlock()).number);
  assert.equal(repaired.shortfallAfterPremium, 0n);
  assert.ok(repaired.premium <= BigInt(v.repair_premium) && repaired.premium > BigInt(v.repair_premium) - 10n);
  assert.ok(repaired.maxSpendable <= BigInt(v.repair_max_spendable) && repaired.maxSpendable > BigInt(v.repair_max_spendable) - 10n);
  assert.deepEqual(repaired.reasons, []);
  // A real repair premium can make the aggregate-coverage estimate look
  // worthwhile even when the exact quoted refinance loses money.
  const addedMark = Number(repaired.afterCoverage - repaired.beforeCoverage) / Number(RAW);
  const terms = { debt: 10, rate: .12, face: 100, premium_bp: 100, joined: true, payback_y: 10,
    k: Number(repaired.currentK) / 1e18, headroom: 0, unactivated: addedMark, fixed_cost: 0, penalty_months: 0,
    risk: 0, opp_rate: .12, buck_value: 1, expected_relief_rate: .02, premium_refund_fraction: 1,
    expected_refund_growth: -.02, expected_wallet_growth: 0, wallet_already_issued: true,
    activated_capacity: 0, issuance_limit: 1000 };
  const naive = refinance(terms, 1, 1);
  const fullRefund = await ops.quotePremiumRefund(depreciatingId, repaired.coverage, { blockNumber: repaired.blockNumber });
  assert.equal(fullRefund.reason, "cash-only");
  assert.equal(fullRefund.premium, repaired.premium);
  const accurate = refinanceQuoted(terms, 1, 0, 1, addedMark, Number(repaired.premium) / Number(RAW),
    { premiumRefundFraction: Number(fullRefund.refundFractionWad) / 1e18 });
  assert.ok(naive.go && !accurate.go && Math.abs(accurate.deposit - 1.8) < .00001);
  // The menu shares identical reads within its block, including concurrent
  // candidates, while keeping a separate exact quote for each coverage.
  const reads = [];
  const trackedSession = { ...s, client: { ...s.client, readContract: (request) => {
    reads.push(request);
    return s.client.readContract(request);
  } } };
  const trackedOps = netCreditOps(trackedSession, stack);
  const menu = await trackedOps.issueCoverageMenu(debtor.address, depreciatingId, requested, 2n * RAW,
    { reserveBuffer: BigInt(V.reserve_buffer) });
  assert.ok(menu.length <= 15 && menu.every((item) => item.blockNumber === repaired.blockNumber));
  const readKeys = reads.map((r) => JSON.stringify([r.address.toLowerCase(), r.functionName, r.args],
    (_, value) => typeof value === "bigint" ? `bigint:${value}` : value));
  assert.equal(new Set(readKeys).size, reads.length);
  assert.equal(reads.filter((r) => r.functionName === "quoteIssue").length, menu.length);
  assert.equal(reads.filter((r) => r.functionName === "currentBuckK").length, 1);
  const ready = menu.filter((item) => !item.reasons.length);
  const refunds = await Promise.all(ready.map((item) =>
    ops.quotePremiumRefund(depreciatingId, item.coverage, { blockNumber: item.blockNumber })));
  const choices = ready.map((item, i) => {
    const refund = refunds[i];
    assert.equal(refund.blockNumber, item.blockNumber);
    assert.equal(refund.premium, item.premium);
    assert.equal(refund.reason, "cash-only");
    assert.equal(refund.pool.toLowerCase(), item.premiumPool.toLowerCase());
    return { usd: 1, wallet_used: 0, new_spendable: 1,
      present_coverage: Number(item.afterCoverage - item.beforeCoverage) / Number(RAW),
      premium: Number(item.premium) / Number(RAW), premium_refund_fraction: Number(refund.refundFractionWad) / 1e18 };
  });
  const selected = bestQuotedRefinance(terms, choices);
  assert.notEqual(selected.index, null);
  assert.ok(selected.verdict.go);
  const chosen = ready[selected.index];
  assert.ok(chosen.coverage > 30n * RAW && chosen.coverage < 32n * RAW && chosen.premium < repaired.premium);
  // Narrow the maximum to the economically priced premium before submitting.
  const previous = await ops.facility(depreciatingId);
  const resultIssue = await ops.issueCandidate(debtor.address, { ...chosen, maxPremium: chosen.premium });
  const updated = await ops.facility(depreciatingId);
  assert.equal(updated.activated, previous.activated + chosen.coverage);
  assert.equal(updated.issued, previous.issued + resultIssue.principal);
  assert.ok(resultIssue.premium <= chosen.premium);
  const fresh = await trackedOps.assessIssueCandidate(debtor.address, depreciatingId, 0n, requested, 0n);
  assert.equal(fresh.priorPrincipal, updated.issued); // cache does not survive a later operation
  assert.deepEqual(s.mismatches, []);
});
