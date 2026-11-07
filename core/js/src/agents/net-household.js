// Checkpointed canonical refinancing; mirrored by sim/net_household.py.
// The caller supplies an assessed, funded economic plan. Existing SDKs enforce
// issuance/trade/payment bounds and authenticate each receipt. Only the exact
// bank payment changes the mortgage; an uncertain phase never sends twice.
import { IssuancePending } from "../net-world.js";
import { sellBuck, netSaleFromReceipt } from "../market.js";
import { TransactionReceiptPending, TransactionReverted } from "../session.js";
import { markNetBasketPositions } from "../basket.js";

const RAW = 1_000_000n, MAX = (1n << 256n) - 1n;
const PHASES = ["issue", "export", "mortgage"];
const BANK = "0x000000000000000000000000000000000000dEaD";
const snake = (key) => key.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`);
const wire = (value) => {
  if (typeof value === "bigint") return String(value);
  if (typeof value === "number" && !Number.isFinite(value)) throw new Error("non-finite household event value");
  if (value === null || ["string", "number", "boolean"].includes(typeof value)) return value;
  if (Array.isArray(value)) return value.map(wire);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, wire(v)]));
  throw new Error("unsupported household event value");
};

/** One serialized local-EVM operation and append-only public event stream.
 * It is neither an economic optimizer nor a complete saved-world importer.
 * The household owns its calendar and counterfactual; receipt evidence does
 * not establish chain finality. All event money uses decimal strings. */
export class HouseholdRefinance {
  constructor(agent) { this.agent = agent; this.flow = null; this.events = []; this.decisions = 0; }
  get active() { return this.flow !== null && ["running", "pending"].includes(this.flow.status); }
  status() {
    const f = this.flow;
    return { schema: 1, decision: this.decisions, status: f?.status ?? "idle", phase: f?.phase ?? null,
      hash: f?.hash ?? null, events: this.events.length };
  }
  async accounts(world) {
    const a = this.agent, owner = world.holderAddress(a.account);
    try {
      const s = world.session, blockNumber = (await s.client.getBlock()).number;
      const at = (c, functionName, args) => s.client.readContract({ address: c.address, abi: c.abi, functionName, args, blockNumber });
      const cash = await at(world.usdc, "balanceOf", [owner]), bucks = await at(world.buck, "balanceOf", [owner]);
      let issued = 0n, premium = 0n;
      const facilities = [];
      for (const id of world._credits.get(a.account.address) ?? []) {
        const [principal, seconds] = await at(world.credit, "issuanceOf", [id]);
        const deposit = await at(world.buck, "premiumPrincipal", [id]);
        facilities.push({ token_id: id, issued: principal, claim_seconds: seconds, premium_nominal: deposit });
        issued += principal; premium += deposit;
      }
      const limit = await at(world.buck, "creditLimit", [owner]);
      const buffer = this.flow?.plan.cash_buffer ?? a.buffer, reserve = this.flow?.plan.reserve_floor ?? 0n, custody = {};
      for (const [name, proxy] of [["savings", a.savings], ["wallet", a.wallet]]) if (proxy) {
        custody[name] = { usdc: await at(world.usdc, "balanceOf", [proxy.address]),
          buck_scaled: await at(world.buck, "netBalanceScaled", [proxy.address]) };
      }
      return wire({ usdc: cash, buck: bucks, buck_scaled: await at(world.buck, "netBalanceScaled", [owner]),
        issued, credit_limit: limit, custody, facilities, premium_nominal: premium, mortgage: a.mortgage, external_paid: a.externalPaid,
        premium_paid: a.premiumPaid, switch_paid: a.switchPaid, penalty_paid: a.penaltyPaid,
        cash_buffer: buffer, reserve_floor: reserve, ratios: { utilization: limit ? Number(issued) / Number(limit) : null,
          cash_buffer_cover: buffer ? Number(cash) / Number(buffer) : null, reserve_cover: reserve ? Number(bucks) / Number(reserve) : null } });
    } catch (err) { return { unavailable: String(err).slice(0, 200) }; }
  }
  async market(world) {
    try {
      const s = world.session, blockNumber = (await s.client.getBlock()).number;
      const at = (c, functionName, args = []) => s.client.readContract({ address: c.address, abi: c.abi, functionName, args, blockNumber });
      const [sqrt] = await at(world.poolUB, "slot0");
      let numerator = sqrt * sqrt, denominator = 1n << 192n;
      if ((await at(world.poolUB, "token0")).toLowerCase() !== world.buck.address.toLowerCase()) {
        [numerator, denominator] = [denominator, numerator];
      }
      const [gross, net] = await at(world.buck, "accountBalancesScaled", [world.poolUB.address]);
      return wire({ block: blockNumber, k_wad: await at(world.kctrl, "currentBuckK"),
        bvib_wad: await at(world.basket, "basketValueInBuck"), nominal_usdc_per_buck: { numerator, denominator },
        pool_nominal_scaled: gross, pool_net_scaled: net, fee_ppm: BigInt(world.fees.ub) });
    } catch (err) { return { unavailable: String(err).slice(0, 200) }; }
  }
  async basket(world) {
    const a = this.agent;
    if (!a.receipts.length) return { basis: "0", mark: "0", roi: null, positions: [] };
    try {
      const owner = a.savings?.address ?? world.holderAddress(a.account);
      const mark = await markNetBasketPositions(world.session, world.basket, world.buck, owner, a.receipts);
      return wire({ basis: mark.basis, mark: mark.mark, roi: mark.roi,
        positions: mark.positions.map((p) => ({ token_id: p.tokenId, shares: p.shares, basis: p.basis, mark: p.mark })) });
    } catch (err) { return { unavailable: String(err).slice(0, 200) }; }
  }
  async event(world, day, status, detail = {}) {
    let block = null;
    try { block = Number((await world.session.client.getBlock()).number); } catch { /* explicit unavailable */ }
    this.events.push({ schema: 1, seq: this.events.length + 1, member: this.agent.member, decision: this.decisions,
      day, block, phase: this.flow.phase, status, accounts: await this.accounts(world), ...wire(detail) });
  }
  async start(world, given, day, ctr = {}, { context = {} } = {}) {
    if (this.active) throw new Error("household refinancing already active");
    const p = { switch_cost: 0n, penalty_numerator: 0n, penalty_denominator: 1n, ...given };
    const values = [p.export_buck, p.minimum_usdc, p.mortgage_limit, p.cash_buffer, p.reserve_floor,
      p.switch_cost, p.penalty_numerator, p.penalty_denominator];
    if (values.some((v) => typeof v !== "bigint" || v < 0n || v > MAX)
        || [p.export_buck, p.minimum_usdc, p.mortgage_limit, p.penalty_denominator].some((v) => v <= 0n)
        || p.mortgage_limit > p.minimum_usdc || p.mortgage_limit > this.agent.mortgage
        || p.cash_buffer < this.agent.buffer) {
      throw new Error("invalid household refinance bounds");
    }
    const a = this.agent, owner = world.holderAddress(a.account), ids = world._credits.get(a.account.address) ?? [];
    const c = p.candidate;
    if (c && (!Array.isArray(c.reasons) || c.reasons.length || !ids.includes(c.tokenId))) {
      throw new Error("unavailable household issuance candidate");
    }
    if (c && p.reserve_floor < c.reserveRequired + c.reserveBuffer) throw new Error("refinance must retain the quoted BUCK reserve");
    if (!c && p.switch_cost) throw new Error("wallet prepayment cannot switch insurance");
    const book = await world.netCreditOps.household(owner, ids);
    for (const id of ids) if ((await world.session.call(world.buck, "reductionAuthorization", [id]))[0]) {
      throw new Error("household reduction authorization pending");
    }
    if (book.spendable + (c?.requestedSpendable ?? 0n) < p.export_buck + p.reserve_floor) {
      throw new Error("refinance would consume the BUCK reserve");
    }
    const penalty = (p.mortgage_limit * p.penalty_numerator + p.penalty_denominator - 1n) / p.penalty_denominator;
    if (p.mortgage_limit + p.switch_cost + penalty > MAX) throw new Error("household bank allocation overflows uint256");
    const cash = await world.session.call(world.usdc, "balanceOf", [owner]);
    if (cash + p.minimum_usdc < p.cash_buffer + p.switch_cost + p.mortgage_limit + penalty) {
      throw new Error("refinance would consume the USDC buffer");
    }
    context = wire(context);
    this.decisions++;
    this.counters = ctr;
    this.flow = { plan: { ...p, candidate: c ? { ...c, reasons: [...c.reasons] } : null },
      phase: c ? "issue" : "export", status: "running", hash: null,
      initial_joined: a.joined, terms: null };
    a.fundingPending = true;
    const limits = Object.fromEntries(["surplus_policy", "theta", "issue_premium", "retire_disc", "utilization", "max_impact_bp"]
      .map((k) => [k, a.p[k]]));
    const plan = { ...this.flow.plan, candidate: c ? Object.fromEntries(Object.entries(c).map(([k, v]) => [snake(k), v])) : null };
    await this.event(world, day, "planned", { plan, context, limits, market: await this.market(world), basket: await this.basket(world) });
    return this.advance(world, day, ctr);
  }
  async refuse(world, day, reason) {
    this.flow.status = "refused";
    this.agent.fundingPending = false; this.agent.lastError = reason.slice(0, 200);
    this.agent.lastAction = "refinance-refused";
    await this.event(world, day, "refused", { reason: reason.slice(0, 200), transaction_hash: this.flow.hash });
  }
  async apply(world, day, result, ctr) {
    const a = this.agent, f = this.flow, phase = f.phase;
    const receipt = result.receipt ?? { transactionHash: result.transactionHash, blockNumber: result.blockNumber };
    let amounts;
    if (phase === "issue") {
      a.premiumPaid += result.premium; a.joined = true;
      amounts = { principal: result.principal, premium: result.premium, spendable: result.spendable };
    } else if (phase === "export") {
      a.tradeLoss += result.paid - result.received;
      ctr.rtrBuckSold = (ctr.rtrBuckSold ?? 0n) + result.paid / RAW;
      amounts = { buck: result.paid, nominal_buck: result.nominalPaid, usdc: result.received };
    } else {
      const t = f.terms;
      if (result.amount !== t.amount || a.mortgage < t.principal) throw new Error("household bank allocation changed");
      a.mortgage -= t.principal; a.externalPaid += t.principal; a.switchPaid += t.switch; a.penaltyPaid += t.penalty;
      a.refis++; a.deploys++;
      const key = f.initial_joined ? "rtrTopUps" : "rtrRefis";
      ctr[key] = (ctr[key] ?? 0) + 1;
      ctr.rtrUsdRetired = (ctr.rtrUsdRetired ?? 0n) + t.principal / RAW;
      amounts = { usdc: result.amount, principal_usdc: t.principal, switch_usdc: t.switch, penalty_usdc: t.penalty };
    }
    // Commit financial effects/cursor before optional account observations.
    f.hash = null; f.terms = null; f.status = "running";
    f.phase = phase === "mortgage" ? "complete" : PHASES[PHASES.indexOf(phase) + 1];
    await this.event(world, day, "confirmed", { operation: phase, amounts,
      transaction_hash: receipt.transactionHash, receipt_block: receipt.blockNumber });
  }
  async recover(world) {
    const f = this.flow, a = this.agent, owner = world.holderAddress(a.account), p = f.plan;
    const receipt = await world.session.client.getTransactionReceipt({ hash: f.hash });
    if (receipt?.transactionHash?.toLowerCase() !== f.hash.toLowerCase()) throw new Error("household receipt hash mismatch");
    if (receipt.status === "reverted") throw new TransactionReverted(f.phase, "household operation reverted", receipt);
    if (f.phase === "issue") return world.netCreditOps.issuanceFromReceipt(owner, p.candidate, receipt, f.hash);
    if (f.phase === "export") return netSaleFromReceipt(world, world.netMarket, { address: owner }, receipt,
      { expectedHash: f.hash, expectedInput: p.export_buck, minimumOut: p.minimum_usdc });
    return world.netSettlementOps.paymentFromReceipt(owner, BANK, f.terms.amount, receipt, f.hash);
  }
  async advance(world, day, ctr = this.counters ?? {}) {
    if (!this.active) return false;
    const f = this.flow, a = this.agent, s = world.session, owner = world.holderAddress(a.account);
    if (f.status === "pending") {
      if (!f.hash) { a.lastError = "refinance hash unavailable; reconcile before continuing"; return true; }
      let result;
      try { result = await this.recover(world); }
      catch (err) {
        if (err instanceof TransactionReverted) await this.refuse(world, day, String(err));
        else a.lastError = `refinance receipt unavailable: ${err}`.slice(0, 200);
        return true;
      }
      await this.apply(world, day, result, ctr);
    }
    while (f.phase !== "complete") {
      const p = f.plan, phase = f.phase;
      if (phase === "mortgage") {
        const penalty = (p.mortgage_limit * p.penalty_numerator + p.penalty_denominator - 1n) / p.penalty_denominator;
        const amount = p.mortgage_limit + p.switch_cost + penalty;
        if (await s.call(world.usdc, "balanceOf", [owner]) < amount + p.cash_buffer) {
          await this.refuse(world, day, "refinance USDC buffer changed"); return true;
        }
        f.terms = { amount, principal: p.mortgage_limit, switch: p.switch_cost, penalty };
      } else if (phase === "export" && await s.call(world.buck, "balanceOf", [owner]) < p.export_buck + p.reserve_floor) {
        await this.refuse(world, day, "refinance BUCK reserve changed"); return true;
      }
      f.status = "pending"; f.hash = null;
      let result;
      try {
        if (phase === "issue") result = await world.netCreditOps.issueCandidate(owner, p.candidate);
        else if (phase === "export") result = await sellBuck(world, world.netMarket, { address: owner }, p.export_buck,
          { minOut: p.minimum_usdc });
        else result = await world.netSettlementOps.payExact(owner, BANK, f.terms.amount);
      } catch (err) {
        f.hash = err.transactionHash ?? null;
        const reverted = err instanceof IssuancePending && err.reverted
          || err instanceof TransactionReverted && err.receipt?.status === "reverted";
        if (reverted && f.hash) await this.refuse(world, day, String(err));
        else {
          a.lastAction = "refinance-pending"; a.lastError = String(err).slice(0, 200);
          await this.event(world, day, "pending", { reason: a.lastError, transaction_hash: f.hash });
        }
        return true;
      }
      await this.apply(world, day, result, ctr);
    }
    f.status = "complete"; a.fundingPending = false; a.lastAction = "refinance"; a.lastError = "";
    await this.event(world, day, "complete");
    return true;
  }
}
