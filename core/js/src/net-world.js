// Experimental BuckNet deployment and per-NFT operations.  This module is
// browser-safe: callers inject artifacts, identities and transaction dispatch.
// A deployed stack is not usable until the Carrying fund is bound and a basket
// configured; premium-bearing facilities also need their insurer's own pool.
import { linkArtifact } from "./artifacts.js";
import { TransactionReverted } from "./session.js";
import { isAddress, parseEventLogs } from "viem";

const Q = 10n ** 36n;
const WAD = 10n ** 18n;
const YEAR = 365n * 86400n + 6n * 3600n;
const MAX_BALANCE = 2n ** 79n - 1n; // BuckTypes.MAX_BALANCE, raw BUCK
const MAX_UINT256 = 2n ** 256n - 1n;
const ZERO_ADDRESS = "0x0000000000000000000000000000000000000000";
const min = (...values) => values.reduce((a, b) => a < b ? a : b);
const validId = (id) => (typeof id === "bigint" && id >= 0n) || (Number.isSafeInteger(id) && id >= 0);

/** Apply the account and supply limits shared by zero-premium facility quotes. */
export function boundActivatedCapacity(capacities, heldScaled, unspentScaled, totalIssued) {
  const values = [...capacities, heldScaled, unspentScaled, totalIssued];
  if (values.some((v) => typeof v !== "bigint" || v < 0n)
      || heldScaled + unspentScaled > MAX_BALANCE * Q || totalIssued > MAX_UINT256 / Q) {
    throw new Error("invalid activated capacity inputs");
  }
  const sharedLimit = min((MAX_BALANCE * Q - heldScaled - unspentScaled) / Q, MAX_UINT256 / Q - totalIssued);
  const potentialCapacity = min(capacities.reduce((sum, value) => sum + value, 0n), sharedLimit);
  return { potentialCapacity, sharedLimit };
}

/** Bounded face-coverage menu near the desired new spendable amount. All
 * inputs are exact integers; K is scaled by 1e18. Actual quotes can move up
 * or down with rounding and must be checked individually. */
export function issueCoverageCandidates(face, activated, mark, principal, k, premiumRateBp, requested) {
  if ([face, activated, mark, principal, k, premiumRateBp, requested]
    .some((v) => typeof v !== "bigint" || v < 0n || v > MAX_UINT256)
      || activated > face || mark > face || premiumRateBp * 10n >= 10000n || requested === 0n) {
    throw new Error("invalid issuance coverage inputs");
  }
  const remaining = face - activated;
  if (!remaining || !mark) return [0n];
  const choices = new Set([0n, 1n, remaining]);
  for (let n = 1n; n < 8n; n++) choices.add(remaining * n / 8n);
  const before = mark * activated / face, beforeLimit = before * k / WAD;
  const needed = principal + requested > beforeLimit ? principal + requested - beforeLimit : 0n;
  const spread = k - premiumRateBp * 10n * WAD / 10000n;
  if (needed && spread > 0n) {
    const ceilDiv = (a, b) => (a + b - 1n) / b;
    const estimate = min(remaining, ceilDiv(ceilDiv(needed * WAD, spread) * face, mark));
    const step = ceilDiv(face, mark);
    for (const offset of [-2n, -1n, 0n, 1n, 2n]) {
      const candidate = estimate + offset * step;
      choices.add(candidate < 0n ? 0n : min(candidate, remaining));
    }
  }
  return [...choices].sort((a, b) => a < b ? -1 : a > b ? 1 : 0);
}

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

export class IssuancePending extends Error {
  constructor(candidate, cause, { transactionHash = null, reverted = false } = {}) {
    super(`issuance of facility ${candidate.tokenId} needs transaction reconciliation`, { cause });
    this.name = "IssuancePending";
    this.candidate = candidate;
    this.transactionHash = transactionHash;
    this.reverted = reverted;
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
 *  Issue requires an explicit BigInt maximum premium and expected pool. Quotes are advisory.
 *  Authorization and reduction are separate transactions;
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
    issue: (owner, id, coverage, spendable, maxPremium, expectedPool) => {
      if (typeof maxPremium !== "bigint" || maxPremium < 0n || maxPremium > MAX_UINT256) {
        throw new Error("invalid maximum premium");
      }
      if (typeof expectedPool !== "string" || !isAddress(expectedPool)) throw new Error("invalid expected insurer pool");
      return send(owner, token, "issue", [id, coverage, spendable, maxPremium, expectedPool]);
    },
    quoteReduction: (id, principal) => session.call(fund, "previewRelief", [id, principal]),
    authorizeReduction: (owner, id, principal, minimumReliefScaled, deadline) =>
      send(owner, token, "authorizeReduction", [id, principal, minimumReliefScaled, deadline]),
    reduceAuthorized: (owner, id, principal) => send(owner, fund, "reduce", [id, principal]),
  };
  const validateIssueCandidate = (tokenId, coverage, requestedSpendable, maxPremium, reserveBuffer, requestedBlock) => {
    if (!validId(tokenId) || BigInt(tokenId) > MAX_UINT256 || [coverage, requestedSpendable, maxPremium, reserveBuffer]
      .some((v) => typeof v !== "bigint" || v < 0n || v > MAX_UINT256) || requestedSpendable === 0n
      || (requestedBlock !== null && (typeof requestedBlock !== "bigint" || requestedBlock < 0n))) {
      throw new Error("invalid issuance candidate");
    }
    return BigInt(tokenId);
  };
  // Cache identical reads only within a single, ephemeral block snapshot.
  // Store the promise before awaiting so concurrent choices share it too.
  const issueBlockReader = (blockNumber) => {
    const cache = new Map();
    return (c, functionName, args = []) => {
      const key = JSON.stringify([c.address.toLowerCase(), functionName, args],
        (_, value) => typeof value === "bigint" ? `bigint:${value}` : value);
      if (!cache.has(key)) cache.set(key, session.client.readContract({ address: c.address, abi: c.abi,
        functionName, args, blockNumber }));
      return cache.get(key);
    };
  };
  const assessIssueAt = async (owner, tokenId, coverage, requestedSpendable, maxPremium, reserveBuffer, blockNumber, read) => {
    const address = owner.address ?? owner;
    const [verified, carrying, actualOwner, [face, activated]] = await Promise.all([
      read(stack.registry, "isVerified", [address]), read(stack.registry, "isCarrying", [address]),
      read(credit, "ownerOf", [tokenId]), read(credit, "creditInfo", [tokenId]),
    ]);
    if (!verified || carrying) throw new Error("household owner must be verified and non-Carrying");
    if (actualOwner.toLowerCase() !== address.toLowerCase()) throw new Error("foreign household facility");
    if (coverage > face - activated) throw new Error("issuance candidate exceeds unactivated coverage");
    const [[before], [after, premium, reserveRequired, maxSpendable], [priorPrincipal, age], currentK,
      ownerSpendable, [pending], configuredToken, fundVerified, fundCarrying, maturitiesPending] = await Promise.all([
      read(quoter, "quoteIssue", [tokenId, 0n]), read(quoter, "quoteIssue", [tokenId, coverage]),
      read(credit, "issuanceOf", [tokenId]), read(stack.controller, "currentBuckK"),
      read(token, "balanceOf", [address]), read(token, "reductionAuthorization", [tokenId]),
      read(fund, "token"), read(stack.registry, "isVerified", [fund.address]),
      read(stack.registry, "isCarrying", [fund.address]), read(token, "maturitiesPending"),
    ]);
    if (after < before) throw new Error("issuance candidate reduced current coverage");
    const reasons = [];
    if (configuredToken.toLowerCase() !== token.address.toLowerCase() || !fundVerified || !fundCarrying) reasons.push("fund-unavailable");
    if (maturitiesPending) reasons.push("maturities-pending");
    if (pending) reasons.push("authorization-pending");
    if (priorPrincipal && age >= priorPrincipal * 50n * YEAR) reasons.push("facility-matured");
    let premiumPool = ZERO_ADDRESS;
    if (premium) {
      premiumPool = await read(token, "facilityPool", [tokenId]);
      if (premiumPool.toLowerCase() === ZERO_ADDRESS) {
        const insurer = await read(credit, "insurerOf", [tokenId]);
        premiumPool = await read(token, "insurerPool", [insurer]);
      }
      if (premiumPool.toLowerCase() === ZERO_ADDRESS || !(await read(stack.registry, "isVerified", [premiumPool]))
          || !(await read(stack.registry, "isCarrying", [premiumPool]))) reasons.push("insurer-pool-unavailable");
    }
    if (premium > maxPremium) reasons.push("premium-limit");
    const bufferedReserve = reserveRequired + (premium ? reserveBuffer : 0n);
    const reserveShortfall = bufferedReserve > ownerSpendable ? bufferedReserve - ownerSpendable : 0n;
    if (reserveShortfall) reasons.push("reserve-shortfall");
    if (requestedSpendable > maxSpendable) reasons.push("insufficient-capacity");
    const beforeLimit = before * currentK / WAD, afterLimit = after * currentK / WAD;
    return { blockNumber, tokenId, coverage, requestedSpendable, maxPremium, priorPrincipal,
      beforeCoverage: before, afterCoverage: after, currentK,
      shortfallBefore: priorPrincipal > beforeLimit ? priorPrincipal - beforeLimit : 0n,
      shortfallAfterPremium: priorPrincipal + premium > afterLimit ? priorPrincipal + premium - afterLimit : 0n,
      premium, reserveRequired, reserveBuffer, ownerSpendable, reserveShortfall, maxSpendable, premiumPool, reasons };
  };
  // Assess one caller-selected face-coverage amount at one block. This is
  // neither a coverage search nor an execution/market guarantee. A household
  // pauses for a pending reduction even though issue itself does not require
  // that workflow guard. The extra reserve applies only to a nonzero premium.
  ops.assessIssueCandidate = async (owner, tokenId, coverage, requestedSpendable, maxPremium,
    { reserveBuffer = 0n, blockNumber: requestedBlock = null } = {}) => {
    tokenId = validateIssueCandidate(tokenId, coverage, requestedSpendable, maxPremium, reserveBuffer, requestedBlock);
    const blockNumber = requestedBlock ?? (await session.client.getBlock()).number;
    return assessIssueAt(owner, tokenId, coverage, requestedSpendable, maxPremium, reserveBuffer, blockNumber,
      issueBlockReader(blockNumber));
  };
  // No additional coverage is needed when the zero-coverage quote already
  // passes. Otherwise each bounded candidate is assessed at the same block.
  ops.issueCoverageMenu = async (owner, tokenId, requestedSpendable, maxPremium, { reserveBuffer = 0n } = {}) => {
    tokenId = validateIssueCandidate(tokenId, 0n, requestedSpendable, maxPremium, reserveBuffer, null);
    const blockNumber = (await session.client.getBlock()).number;
    const read = issueBlockReader(blockNumber);
    const zero = await assessIssueAt(owner, tokenId, 0n, requestedSpendable, maxPremium, reserveBuffer, blockNumber, read);
    const common = new Set(["fund-unavailable", "maturities-pending", "authorization-pending", "facility-matured"]);
    if (!zero.reasons.length || zero.reasons.some((reason) => common.has(reason))) return [zero];
    const [[face, activated, rate], mark, [principal], k] = await Promise.all([
      read(credit, "creditInfo", [tokenId]), read(credit, "depreciatedFaceValue", [tokenId]),
      read(credit, "issuanceOf", [tokenId]), read(stack.controller, "currentBuckK"),
    ]);
    const coverage = issueCoverageCandidates(face, activated, mark, principal, k, BigInt(rate), requestedSpendable);
    const others = await Promise.all(coverage.filter(Boolean).map((amount) =>
      assessIssueAt(owner, tokenId, amount, requestedSpendable, maxPremium, reserveBuffer, blockNumber, read)));
    return [zero, ...others];
  };
  // Authenticate one selected facility event and its saved hash. The caller
  // must apply it once, serialize account actions and handle chain finality.
  ops.issuanceFromReceipt = (owner, candidate, receipt, expectedHash) => {
    if (!candidate || receipt?.status !== "success") throw new Error("issuance receipt is not successful");
    if (typeof expectedHash !== "string" || !expectedHash || !receipt.transactionHash
        || receipt.transactionHash.toLowerCase() !== expectedHash.toLowerCase()) {
      throw new Error("issuance transaction hash mismatch");
    }
    const address = owner.address ?? owner;
    const events = parseEventLogs({ abi: token.abi, logs: receipt.logs, eventName: "FacilityIssued" })
      .filter((e) => e.address.toLowerCase() === token.address.toLowerCase());
    if (events.length !== 1) throw new Error("issuance receipt missing or ambiguous");
    const { owner: eventOwner, tokenId, coverage, principal, premium, pool } = events[0].args;
    if (eventOwner.toLowerCase() !== address.toLowerCase() || tokenId !== candidate.tokenId
        || coverage !== candidate.coverage || premium > candidate.maxPremium
        || principal !== candidate.requestedSpendable + premium
        || pool.toLowerCase() !== (premium ? candidate.premiumPool : ZERO_ADDRESS).toLowerCase()) {
      throw new Error("issuance receipt does not match candidate");
    }
    return { tokenId, coverage, spendable: candidate.requestedSpendable, principal, premium, pool, receipt };
  };
  // A quote is not an economic decision or transaction guarantee. Once
  // submission is attempted, even a missing hash is an uncertain outcome.
  ops.issueCandidate = async (owner, candidate) => {
    if (!candidate || !Array.isArray(candidate.reasons) || candidate.reasons.length) {
      throw new Error("issuance candidate is not available");
    }
    const address = owner.address ?? owner;
    if ((await session.call(credit, "ownerOf", [candidate.tokenId])).toLowerCase() !== address.toLowerCase()) {
      throw new Error("foreign household facility");
    }
    if ((await session.call(token, "reductionAuthorization", [candidate.tokenId]))[0]) {
      throw new Error("reduction authorization pending");
    }
    let receipt;
    try {
      receipt = await ops.issue(owner, candidate.tokenId, candidate.coverage, candidate.requestedSpendable,
        candidate.maxPremium, candidate.premiumPool);
      if (receipt?.status !== "success" || !receipt.transactionHash) throw new IssuancePending(candidate, null,
        { transactionHash: receipt?.transactionHash ?? null,
          reverted: receipt?.status === "reverted" && Boolean(receipt?.transactionHash) });
      return ops.issuanceFromReceipt(owner, candidate, receipt, receipt.transactionHash);
    } catch (err) {
      if (err instanceof IssuancePending) throw err;
      throw new IssuancePending(candidate, err, { transactionHash: err?.transactionHash ?? receipt?.transactionHash ?? null,
        reverted: err instanceof TransactionReverted && err.receipt.status === "reverted" && Boolean(err.transactionHash) });
    }
  };
  // Quote each NFT and its present unactivated mark at the same block. The
  // shared account/supply limit is applied once, not once per facility, and
  // a fully aged facility cannot support another issue. K and other contract
  // conditions can change before a later issue transaction.
  ops.quoteHouseholdActivatedCapacity = async (owner, tokenIds) => {
    const address = owner.address ?? owner, ids = tokenIds.map((id) => {
      if (!validId(id)) throw new Error("invalid household facility");
      return BigInt(id);
    });
    if (new Set(ids.map(String)).size !== ids.length) throw new Error("duplicate household facility");
    const blockNumber = (await session.client.getBlock()).number;
    const read = (c, functionName, args = []) => session.client.readContract({ address: c.address, abi: c.abi,
      functionName, args, blockNumber });
    const [verified, carrying] = await Promise.all([read(stack.registry, "isVerified", [address]),
      read(stack.registry, "isCarrying", [address])]);
    if (!verified || carrying) throw new Error("household owner must be verified and non-Carrying");
    const owners = await Promise.all(ids.map((id) => read(credit, "ownerOf", [id])));
    if (owners.some((actual) => actual.toLowerCase() !== address.toLowerCase())) throw new Error("foreign household facility");
    const quoted = await Promise.all(ids.map(async (tokenId) => {
      const [[present, premium, reserve, maxSpendable], [principal, age], mark] = await Promise.all([
        read(quoter, "quoteIssue", [tokenId, 0n]), read(credit, "issuanceOf", [tokenId]),
        read(credit, "depreciatedFaceValue", [tokenId]),
      ]);
      if (premium || reserve) throw new Error("zero-coverage quote unexpectedly requires a premium or reserve");
      if (present > mark) throw new Error("activated coverage exceeds current face mark");
      const unactivatedPresent = principal > 0n && age >= principal * 50n * YEAR ? 0n : mark - present;
      return { facility: { tokenId, maxSpendable }, unactivatedPresent };
    }));
    const facilities = quoted.map((item) => item.facility);
    const unactivatedPresent = quoted.reduce((sum, item) => sum + item.unactivatedPresent, 0n);
    const [[held, unspent], totalIssued, maturitiesPending] = await Promise.all([
      read(token, "heldAndUnspentScaled", [address]), read(token, "totalIssued"), read(token, "maturitiesPending"),
    ]);
    const { potentialCapacity, sharedLimit } = boundActivatedCapacity(facilities.map((f) => f.maxSpendable), held, unspent, totalIssued);
    return { blockNumber, facilities, unactivatedPresent, sharedLimit, potentialCapacity,
      capacity: maturitiesPending ? 0n : potentialCapacity, maturitiesPending };
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
  ops.quotePremiumRefund = async (tokenId, coverage, { blockNumber: requestedBlock = null } = {}) => {
    if (!validId(tokenId) || typeof coverage !== "bigint" || coverage < 0n
        || (requestedBlock !== null && (typeof requestedBlock !== "bigint" || requestedBlock < 0n))) {
      throw new Error("invalid premium sample");
    }
    tokenId = BigInt(tokenId);
    const blockNumber = requestedBlock ?? (await session.client.getBlock()).number;
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
    catch (err) { throw new ReductionPending(plan, err, { phase: "authorization", transactionHash: err?.transactionHash ?? null,
      reverted: err instanceof TransactionReverted && err.receipt.status === "reverted" && Boolean(err.transactionHash) }); }
    if (authorized?.status !== "success") throw new ReductionPending(plan, null, { phase: "authorization",
      transactionHash: authorized?.transactionHash ?? null,
      reverted: authorized?.status === "reverted" && Boolean(authorized?.transactionHash) });
    try { ops.authorizationFromReceipt(owner, plan, deadline, authorized); }
    catch (err) { throw new ReductionPending(plan, err, { phase: "authorization", transactionHash: authorized.transactionHash ?? null }); }
    let receipt;
    try { receipt = await ops.reduceAuthorized(owner, plan.tokenId, plan.principal); }
    catch (err) { throw new ReductionPending(plan, err, { transactionHash: err?.transactionHash ?? null,
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
      pendingReverted: false, pendingPhase: null, error: null, settlements: [] };
    for (const tokenId of tokenIds) {
      if (result.retired >= requested) break;
      let plan;
      try { plan = await ops.planReduction(owner, tokenId, requested - result.retired, options); }
      catch (err) { result.status = "plan-unavailable"; result.blockedTokenId = tokenId; result.error = String(err).slice(0, 200); break; }
      if (plan.reason === "no-principal") continue;
      if (plan.reason !== "ready") { result.status = plan.reason; result.blockedTokenId = tokenId; break; }
      let settled;
      try { settled = await ops.settlePlan(owner, plan, deadline); }
      catch (err) {
        // settlePlan wraps all exceptions after submission in ReductionPending;
        // another exception is from its preflight, before a transaction.
        if (!(err instanceof ReductionPending)) {
          result.status = "preflight-unavailable"; result.blockedTokenId = tokenId;
          result.error = String(err).slice(0, 200); break;
        }
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
