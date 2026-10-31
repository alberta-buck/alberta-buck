// Experimental BuckNet deployment and per-NFT operations.  This module is
// browser-safe: callers inject artifacts, identities and transaction dispatch.
// A deployed stack is not usable until the Carrying fund is bound and a basket
// configured; premium-bearing facilities also need their insurer's own pool.
import { linkArtifact } from "./artifacts.js";
import { TransactionReverted } from "./session.js";
import { parseEventLogs } from "viem";

const Q = 10n ** 36n;
const WAD = 10n ** 18n;
const YEAR = 365n * 86400n + 6n * 3600n;
const ZERO_ADDRESS = "0x0000000000000000000000000000000000000000";
const min = (...values) => values.reduce((a, b) => a < b ? a : b);
const validId = (id) => (typeof id === "bigint" && id >= 0n) || (Number.isSafeInteger(id) && id >= 0);

export class ReductionPending extends Error {
  constructor(plan, cause, { transactionHash = null, reverted = false, phase = "settlement" } = {}) {
    super(`${phase} of facility ${plan.tokenId} needs transaction reconciliation`, { cause });
    this.name = "ReductionPending";
    this.plan = plan;
    this.transactionHash = transactionHash;
    this.reverted = reverted;
    this.phase = phase;
  }
}

/** Sum authenticated BuckNet delivery events for one transfer direction.
 *  netScaled uses 1e36 fractional units per raw BUCK; a zero net value is
 *  valid, so callers must check count rather than treating zero as missing. */
export function netTransferValue(token, receipt, from, to) {
  const same = (a, b) => a.toLowerCase() === b.toLowerCase();
  const events = parseEventLogs({ abi: token.abi, logs: receipt.logs, eventName: "NetValueTransfer" })
    .filter((e) => same(e.address, token.address) && same(e.args.from, from) && same(e.args.to, to));
  return events.reduce((out, e) => ({ count: out.count + 1, nominal: out.nominal + e.args.nominal,
    netScaled: out.netScaled + e.args.netScaled }), { count: 0, nominal: 0n, netScaled: 0n });
}

export async function deployNetStack(session, artifacts, { registry, credit, controller, governance,
                                                         creditDeployer, registryGovernor } = {}) {
  const gov = governance?.address ?? governance ?? session.account.address;
  const deploy = (name, args = []) => session.deploy(artifacts(name), args, { name });
  const fund = await deploy("JubileeFund", [gov]);
  const maturityLibrary = await deploy("BuckNetMaturity");
  const transfersLibrary = await deploy("BuckNetTransfers");
  const linked = linkArtifact(artifacts("BuckNet"), {
    "src/lib/BuckNetMaturity.sol:BuckNetMaturity": maturityLibrary.address,
    "src/lib/BuckNetTransfers.sol:BuckNetTransfers": transfersLibrary.address,
  });
  const token = await session.deploy(linked, [credit.address, controller.address, registry.address, fund.address],
    { name: "BuckNet" });
  const quoter = await deploy("BuckNetQuoter", [token.address]);
  await session.send(credit, "setBuck", [token.address], { account: creditDeployer });
  await session.send(registry, "setBuck", [token.address], { account: registryGovernor });
  return { registry, credit, controller, fund, token, quoter, maturityLibrary, transfersLibrary };
}

/** Actual per-facility amounts; never synthesize a signed debt from a wallet.
 *  execute(owner, contract, method, args) can dispatch through an agent proxy.
 *  Quotes are advisory. Authorization and reduction are separate transactions;
 *  a failed reduction leaves its prior authorization to retry or cancel. */
export function netCreditOps(session, stack, execute = null) {
  const { credit, token, fund, quoter } = stack;
  const send = execute ?? ((owner, target, name, args) => session.send(target, name, args, { account: owner }));
  const readFacility = async (tokenId, knownSpendable) => {
    const owner = await session.call(credit, "ownerOf", [tokenId]);
    const [[face, activated, premiumRate], [issued, claimSeconds], premiumNominal, insurerPool, spendable] =
      await Promise.all([session.call(credit, "creditInfo", [tokenId]), session.call(credit, "issuanceOf", [tokenId]),
        session.call(token, "premiumPrincipal", [tokenId]), session.call(token, "facilityPool", [tokenId]),
        knownSpendable ?? session.call(token, "balanceOf", [owner])]);
    return { tokenId, owner, face, activated, premiumRate, issued, claimSeconds, premiumNominal, insurerPool, spendable };
  };
  const facility = (tokenId) => readFacility(tokenId);
  const quoteIssue = async (tokenId, coverage) => {
    const owner = await session.call(credit, "ownerOf", [tokenId]);
    const [[presentCoverage, premium, reserveRequired, maxSpendable], ownerSpendable] = await Promise.all([
      session.call(quoter, "quoteIssue", [tokenId, coverage]), session.call(token, "balanceOf", [owner]),
    ]);
    const reserveShortfall = reserveRequired > ownerSpendable ? reserveRequired - ownerSpendable : 0n;
    return { presentCoverage, premium, reserveRequired, maxSpendable, ownerSpendable, reserveShortfall };
  };
  const ops = {
    facility, quoteIssue,
    issue: (owner, id, coverage, spendable) => send(owner, token, "issue", [id, coverage, spendable]),
    quoteReduction: (id, principal) => session.call(fund, "previewRelief", [id, principal]),
    authorizeReduction: (owner, id, principal, minimumReliefScaled, deadline) =>
      send(owner, token, "authorizeReduction", [id, principal, minimumReliefScaled, deadline]),
    reduceAuthorized: (owner, id, principal) => send(owner, fund, "reduce", [id, principal]),
  };
  // These are same-block observations, not predictions. Mark failures and
  // maturity backlogs propagate rather than becoming an invented rate.
  ops.quoteReliefRate = async (tokenId, principal) => {
    if (!validId(tokenId) || typeof principal !== "bigint" || principal <= 0n) throw new Error("invalid relief rate sample");
    tokenId = BigInt(tokenId);
    const blockNumber = (await session.client.getBlock()).number;
    const read = (c, functionName, args) => session.client.readContract({ address: c.address, abi: c.abi,
      functionName, args, blockNumber });
    const [selected, total] = await read(token, "selectedClaimSeconds", [tokenId, principal]);
    const [relief, cash, exceeds] = await read(fund, "previewRelief", [tokenId, principal]);
    return { blockNumber, selectedSeconds: selected, totalSeconds: total, reliefScaled: relief, cashScaled: cash,
      exceedsLiquidityLimit: exceeds, annualRateWad: selected ? relief * YEAR * WAD / (selected * Q) : null,
      capped: selected > 0n && relief === principal * Q };
  };
  // A proposed full-value premium dilutes only the current pool account in
  // this estimate. Outside positions and later liquidity actions are unknown.
  ops.quotePremiumRefund = async (tokenId, coverage) => {
    if (!validId(tokenId) || typeof coverage !== "bigint" || coverage < 0n) throw new Error("invalid premium sample");
    tokenId = BigInt(tokenId);
    const blockNumber = (await session.client.getBlock()).number;
    const read = (c, functionName, args) => session.client.readContract({ address: c.address, abi: c.abi,
      functionName, args, blockNumber });
    const [, premium] = await read(quoter, "quoteIssue", [tokenId, coverage]);
    const result = (pool, grossScaled, netScaled, refundFractionWad, reason) =>
      ({ blockNumber, premium, pool, grossScaled, netScaled, refundFractionWad, reason });
    if (!premium) return result(null, 0n, 0n, null, "no-premium");
    let pool = await read(token, "facilityPool", [tokenId]);
    if (pool.toLowerCase() === ZERO_ADDRESS) {
      const insurer = await read(credit, "insurerOf", [tokenId]);
      pool = await read(token, "insurerPool", [insurer]);
    }
    if (pool.toLowerCase() === ZERO_ADDRESS) return result(null, 0n, 0n, null, "no-insurer-pool");
    const [verified, carrying] = await Promise.all([read(stack.registry, "isVerified", [pool]),
      read(stack.registry, "isCarrying", [pool])]);
    if (!verified || !carrying) return result(pool, 0n, 0n, null, "invalid-insurer-pool");
    const [gross, net] = await read(token, "accountBalancesScaled", [pool]);
    const fraction = (net + premium * Q) * WAD / (gross + premium * Q);
    return result(pool, gross, net, fraction, "cash-only");
  };
  ops.cancelReduction = (owner, id) => ops.authorizeReduction(owner, id, 0n, 0n, 0n);
  ops.household = async (owner, tokenIds) => {
    const address = owner.address ?? owner;
    if (new Set(tokenIds.map(String)).size !== tokenIds.length) throw new Error("duplicate household facility");
    if (await session.call(stack.registry, "isCarrying", [address])) throw new Error("household wallet must be non-Carrying");
    const spendable = await session.call(token, "balanceOf", [address]);
    const facilities = await Promise.all(tokenIds.map((tokenId) => readFacility(tokenId, spendable)));
    if (facilities.some((f) => f.owner.toLowerCase() !== address.toLowerCase())) throw new Error("foreign household facility");
    return { spendable,
      issued: facilities.reduce((n, f) => n + f.issued, 0n),
      unactivated: facilities.reduce((n, f) => n + f.face - f.activated, 0n),
      premiumNominal: facilities.reduce((n, f) => n + f.premiumNominal, 0n), facilities };
  };
  // A wallet is money, never signed debt. Size one selected NFT reduction
  // against the minimum accepted relief and actual fund liquidity; do not
  // assume the insurer's nominal premium refund will cover the debit.
  ops.planReduction = async (owner, tokenId, requested, { buffer = 1n, reliefSlippageBp = 100 } = {}) => {
    if (!((typeof tokenId === "bigint" && tokenId >= 0n)
          || (Number.isSafeInteger(tokenId) && tokenId >= 0))
        || typeof requested !== "bigint" || typeof buffer !== "bigint"
        || requested < 0n || buffer < 0n || !Number.isInteger(reliefSlippageBp)
        || reliefSlippageBp < 0 || reliefSlippageBp > 10000) throw new Error("invalid reduction budget or relief tolerance");
    tokenId = BigInt(tokenId);
    const address = owner.address ?? owner;
    if (await session.call(stack.registry, "isCarrying", [address])) throw new Error("household wallet must be non-Carrying");
    const f = await facility(tokenId);
    if (f.owner.toLowerCase() !== address.toLowerCase()) throw new Error("foreign household facility");
    const [pending] = await session.call(token, "reductionAuthorization", [tokenId]);
    const result = (principal, reliefScaled, minimumReliefScaled, cashScaled, reason) =>
      ({ tokenId, principal, reliefScaled, minimumReliefScaled, cashScaled, reason });
    if (pending) return result(0n, 0n, 0n, 0n, "authorization-pending");
    let amount = min(requested, f.issued), reason = "quote-unstable";
    const budget = (f.spendable > buffer ? f.spendable - buffer : 0n) * Q;
    if (!amount) return result(0n, 0n, 0n, 0n, "no-principal");
    for (let i = 0; i < 16; i++) {
      const [relief, cash, exceeds] = await ops.quoteReduction(tokenId, amount);
      const floor = relief * BigInt(10000 - reliefSlippageBp) / 10000n, need = amount * Q - floor;
      let candidate = amount;
      if (exceeds) { candidate = min(candidate, amount * (cash / 10n) / relief); reason = "fund-liquidity"; }
      if (need > budget) { candidate = min(candidate, amount * budget / need); reason = "owner-funds"; }
      if (candidate === amount) return result(amount, relief, floor, cash, "ready");
      if (!candidate) return result(0n, relief, floor, cash, reason);
      amount = candidate;
    }
    return result(0n, 0n, 0n, 0n, reason);
  };
  ops.reductionFromReceipt = (owner, tokenId, expectedPrincipal, receipt, expectedHash = null) => {
    if (receipt?.status !== "success") throw new Error("settlement receipt is not successful");
    if (expectedHash !== null && (!receipt.transactionHash
        || receipt.transactionHash.toLowerCase() !== expectedHash.toLowerCase())) {
      throw new Error("settlement transaction hash mismatch");
    }
    const address = owner.address ?? owner;
    const events = parseEventLogs({ abi: token.abi, logs: receipt.logs, eventName: "FacilityReduced" })
      .filter((e) => e.address.toLowerCase() === token.address.toLowerCase()
        && e.args.owner.toLowerCase() === address.toLowerCase() && e.args.tokenId === BigInt(tokenId));
    if (events.length !== 1 || events[0].args.principal !== expectedPrincipal) {
      throw new Error("settlement receipt missing or ambiguous");
    }
    const { principal, reliefScaled, premiumRefund } = events[0].args;
    return { principal, reliefScaled, ownerCostScaled: principal * Q - reliefScaled, premiumNominal: premiumRefund, receipt };
  };
  // An authorization permits a future reduction; it does not retire debt.
  ops.authorizationFromReceipt = (owner, plan, deadline, receipt, expectedHash = null) => {
    if (receipt?.status !== "success") throw new Error("authorization receipt is not successful");
    if (expectedHash !== null && (!receipt.transactionHash
        || receipt.transactionHash.toLowerCase() !== expectedHash.toLowerCase())) {
      throw new Error("authorization transaction hash mismatch");
    }
    const address = owner.address ?? owner;
    const events = parseEventLogs({ abi: token.abi, logs: receipt.logs, eventName: "ReductionAuthorized" })
      .filter((e) => e.address.toLowerCase() === token.address.toLowerCase()
        && e.args.owner.toLowerCase() === address.toLowerCase() && e.args.tokenId === BigInt(plan.tokenId));
    if (events.length !== 1 || events[0].args.principal !== plan.principal
        || events[0].args.minimumReliefScaled !== plan.minimumReliefScaled || events[0].args.deadline !== BigInt(deadline)) {
      throw new Error("authorization receipt missing or ambiguous");
    }
    return events[0];
  };
  ops.settlePlan = async (owner, plan, deadline) => {
    if (plan.reason !== "ready" || plan.principal <= 0n) throw new Error("reduction plan is not ready");
    const [pending] = await session.call(token, "reductionAuthorization", [plan.tokenId]);
    if (pending) throw new Error("reduction authorization already pending");
    let authorized;
    try { authorized = await ops.authorizeReduction(owner, plan.tokenId, plan.principal, plan.minimumReliefScaled, deadline); }
    catch (err) { throw new ReductionPending(plan, err, { phase: "authorization", transactionHash: err.transactionHash ?? null,
      reverted: err instanceof TransactionReverted && err.receipt.status === "reverted" && Boolean(err.transactionHash) }); }
    if (authorized?.status !== "success") throw new ReductionPending(plan, null, { phase: "authorization",
      transactionHash: authorized?.transactionHash ?? null,
      reverted: authorized?.status === "reverted" && Boolean(authorized?.transactionHash) });
    try { ops.authorizationFromReceipt(owner, plan, deadline, authorized); }
    catch (err) { throw new ReductionPending(plan, err, { phase: "authorization", transactionHash: authorized.transactionHash ?? null }); }
    let receipt;
    try { receipt = await ops.reduceAuthorized(owner, plan.tokenId, plan.principal); }
    catch (err) { throw new ReductionPending(plan, err, { transactionHash: err.transactionHash ?? null,
      reverted: err instanceof TransactionReverted && err.receipt.status === "reverted" && Boolean(err.transactionHash) }); }
    if (receipt?.status !== "success") throw new ReductionPending(plan, null,
      { transactionHash: receipt?.transactionHash ?? null,
        reverted: receipt?.status === "reverted" && Boolean(receipt?.transactionHash) });
    try {
      return ops.reductionFromReceipt(owner, plan.tokenId, plan.principal, receipt);
    } catch (err) { throw new ReductionPending(plan, err, { transactionHash: receipt.transactionHash ?? null }); }
  };
  // Each NFT is a separate transaction. Report completed events even if a
  // later authorization remains pending; a purchase is never a repayment.
  ops.retireHousehold = async (owner, tokenIds, requested, deadline, options = {}) => {
    const { buffer = 1n, reliefSlippageBp = 100 } = options;
    const validDeadline = (typeof deadline === "bigint" && deadline >= 0n && deadline < 2n ** 64n)
      || (Number.isSafeInteger(deadline) && deadline >= 0);
    if (typeof requested !== "bigint" || requested < 0n || !validDeadline || typeof buffer !== "bigint" || buffer < 0n
        || !Number.isInteger(reliefSlippageBp) || reliefSlippageBp < 0 || reliefSlippageBp > 10000) {
      throw new Error("invalid reduction budget or relief tolerance");
    }
    await ops.household(owner, tokenIds); // reject duplicates and foreign NFTs before sending anything
    const result = { requested, retired: 0n, reliefScaled: 0n, ownerCostScaled: 0n,
      premiumNominal: 0n, status: "complete", blockedTokenId: null, pendingPrincipal: null,
      pendingMinimumReliefScaled: null, pendingDeadline: null, pendingTransactionHash: null,
      pendingReverted: false, pendingPhase: null, settlements: [] };
    for (const tokenId of tokenIds) {
      if (result.retired >= requested) break;
      const plan = await ops.planReduction(owner, tokenId, requested - result.retired, options);
      if (plan.reason === "no-principal") continue;
      if (plan.reason !== "ready") { result.status = plan.reason; result.blockedTokenId = tokenId; break; }
      let settled;
      try { settled = await ops.settlePlan(owner, plan, deadline); }
      catch (err) {
        if (!(err instanceof ReductionPending)) throw err;
        result.status = "authorization-pending"; result.blockedTokenId = tokenId; result.pendingPrincipal = plan.principal;
        result.pendingMinimumReliefScaled = plan.minimumReliefScaled; result.pendingDeadline = BigInt(deadline);
        result.pendingTransactionHash = err.transactionHash; result.pendingReverted = err.reverted;
        result.pendingPhase = err.phase; break;
      }
      result.settlements.push(settled);
      result.retired += settled.principal;
      result.reliefScaled += settled.reliefScaled;
      result.ownerCostScaled += settled.ownerCostScaled;
      result.premiumNominal += settled.premiumNominal;
    }
    if (result.status === "complete" && result.retired < requested) result.status = "partial";
    return result;
  };
  return ops;
}
