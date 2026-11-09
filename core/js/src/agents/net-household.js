// Checkpointed canonical refinancing; mirrored by sim/net_household.py.
// The caller supplies an assessed economic plan and optional reserve purchase. Existing SDKs enforce
// issuance/trade/payment bounds and authenticate each receipt. Only the exact
// bank payment changes the mortgage; an uncertain phase never sends twice.
import { IssuancePending } from "../net-world.js";
import { buyBuckExact, netPurchaseFromReceipt, sellBuck, netSaleFromReceipt } from "../market.js";
import { TransactionReceiptPending, TransactionReverted } from "../session.js";
import { markNetBasketPositions } from "../basket.js";
import { observeNetMarket, netPurchaseCost, netSaleExpected, netSaleInput } from "../net-market-quote.js";
import { externalPremiumDue, terms, bestQuotedRefinance, stagedRefinance, refinanceQuoted } from "./household-policy.js";

const RAW = 1_000_000n, MAX = (1n << 256n) - 1n;
const PHASES = ["fund", "issue", "export", "mortgage"];
const BANK = "0x000000000000000000000000000000000000dEaD";
const reserveFor = (c) => c.reserveRequired + (c.premium ? c.reserveBuffer : 0n);
const min = (...v) => v.reduce((x, y) => x < y ? x : y), max = (x, y) => x > y ? x : y;
const SIZE_FRACTIONS = [20n, 15n, 10n, 5n, 2n];
const sizes = (cap) => [...new Set(SIZE_FRACTIONS.map((f) => cap * f / 20n))]
  .sort((x, y) => x > y ? -1 : x < y ? 1 : 0);
const bankTerms = (p) => {
  const penalty = (p.mortgage_limit * p.penalty_numerator + p.penalty_denominator - 1n) / p.penalty_denominator;
  return { amount: p.mortgage_limit + p.switch_cost + penalty, principal: p.mortgage_limit, switch: p.switch_cost, penalty };
};
const snake = (key) => key.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`);
const wire = (value) => {
  if (typeof value === "bigint") return String(value);
  if (typeof value === "number" && !Number.isFinite(value)) throw new Error("non-finite household event value");
  if (value === null || ["string", "number", "boolean"].includes(typeof value)) return value;
  if (Array.isArray(value)) return value.map(wire);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, wire(v)]));
  throw new Error("unsupported household event value");
};

/** Forecast inputs for the modeled idle main cash/insurer pool, not monetary
 * balance calculations or promised future ROI. Missing guarded marks fail;
 * no claim age uses a labelled conservative zero relief forecast. */
export async function observeRefinanceForecasts(world) {
  const observation = await world.netCreditOps.quoteGlobalReliefRate(), idleGrowth = Math.expm1(-.02);
  return { values: { expected_relief_rate: observation.annualRateWad === null ? 0 : Number(observation.annualRateWad) / 1e18,
    expected_refund_growth: idleGrowth, expected_wallet_growth: idleGrowth }, observation,
    source: Object.fromEntries(Object.entries(observation).map(([key, value]) => [snake(key), value])),
    basis: observation.annualRateWad === null ? "no-claim-age/zero-relief/idle-cash" : "marked-fund/idle-cash" };
}

/** Read an actual, finite candidate menu without submitting or changing books.
 * Normal external keeper/controller work precedes the read-only assessment.
 * Annual forecasts are explicit. Bank costs use old USDC above the buffer;
 * the sale minimum is mortgage principal, avoiding a second cost deduction.
 * Execution still rechecks the selected issuance and actual trade bounds. */
export async function assessRefinance(a, world, day, { expected_relief_rate, expected_refund_growth,
  expected_wallet_growth, reserve_floor = null, slippage_bp = 50 } = {}) {
  if (world.accounting !== "net" || a.refinanceMachine.active || a.refinanceMachine.settlementDue || a.calendarMachine.active) throw new Error("household refinance assessment needs idle canonical accounting");
  reserve_floor ??= a.refinanceMachine.flow?.plan.reserve_floor ?? 0n;
  if (typeof reserve_floor !== "bigint" || reserve_floor < 0n || reserve_floor > MAX
      || !Number.isInteger(slippage_bp) || slippage_bp < 0 || slippage_bp > 1000) throw new Error("invalid household assessment reserve or slippage");
  const s = world.session, owner = world.holderAddress(a.account), ids = world._credits.get(a.account.address) ?? [];
  const blockNumber = (await s.client.getBlock()).number;
  const at = (c, functionName, args = []) => s.client.readContract({ address: c.address, abi: c.abi, functionName, args, blockNumber });
  const market = await observeNetMarket(s, world.buck, world.usdc, world.poolUB, world.fees.ub, { blockNumber });
  const book = await world.netCreditOps.household(owner, ids, { blockNumber });
  const retained = reserve_floor < book.spendable ? reserve_floor : book.spendable;
  const available = await world.netCreditOps.quoteHouseholdActivatedCapacity(owner, ids);
  if (available.blockNumber !== blockNumber) throw new Error("household assessment block changed");
  const cash = await at(world.usdc, "balanceOf", [owner]), k = await at(world.kctrl, "currentBuckK");
  const bvib = await at(world.basket, "basketValueInBuck"), month = Math.floor((day - a.arrive_day) / 30);
  const state = { accounting: "net", mortgage: Number(a.mortgage) / 1e6, face: Number(a.face) / 1e6,
    joined: a.joined, closing_paid: a.refinanceClosed, k: Number(k) / 1e18, spendable: Number(book.spendable) / 1e6,
    unactivated: available.maturitiesPending ? 0 : Number(available.unactivatedPresent) / 1e6,
    activated_capacity: Number(available.capacity) / 1e6,
    issuance_limit: available.maturitiesPending ? 0 : Number(available.sharedLimit) / 1e6,
    at_renewal: (month - a.p.renew_in) % a.p.term_months === 0,
    basket_usd: Number(bvib) / 1e18 * (Number(market.price_num) / Number(market.price_den)),
    expected_relief_rate, expected_refund_growth, expected_wallet_growth, premium_refund_fraction: 1 };
  const t = terms(state, a.p);
  const utilization = a.face * k * BigInt(Math.trunc(a.p.utilization * 1e9)) / (10n ** 18n * 10n ** 9n);
  const impactPpm = BigInt(Math.trunc(a.p.max_impact_bp * 50));
  let buckCap = market.reserve_buck * impactPpm / 1_000_000n;
  const cashCap = market.reserve_usdc * impactPpm / 1_000_000n;
  if (a.mortgage < market.reserve_usdc) buckCap = min(buckCap, market.reserve_buck * a.mortgage * 1_000_000n /
    ((market.reserve_usdc - a.mortgage) * (1_000_000n - market.fee_ppm)));
  const context = { block: blockNumber, considered: 0, issuance_quotes: 0, rejections: {}, feasible: 0,
    forecasts: { relief_rate: expected_relief_rate, refund_growth: expected_refund_growth, wallet_growth: expected_wallet_growth }, protected_buck: reserve_floor };
  const plans = [], values = [], programmes = [], refunds = new Map(), active = book.facilities.reduce((n, f) => n + f.activated, 0n);
  const annual = a.face * BigInt(a.p.premium_bp) / 10000n;
  const reject = (reason) => { context.rejections[reason] = (context.rejections[reason] ?? 0) + 1; };
  const option = async (candidate, wallet, programme = false) => {
    context.considered++;
    const newly = candidate?.requestedSpendable ?? 0n, premium = candidate?.premium ?? 0n;
    const floor = candidate ? max(retained, reserveFor(candidate)) : retained;
    const buy = candidate ? max(0n, floor - book.spendable) : 0n, cost = netPurchaseCost(market, buy);
    if (cost === null) { reject("reserve-liquidity"); return; }
    const maximum = (cost * BigInt(10000 + slippage_bp) + 9999n) / 10000n, exported = newly + wallet;
    if (exported < RAW || (!programme && exported > buckCap) || maximum > cashCap || buy * market.gross_scaled > buckCap * market.net_scaled) {
      reject("trade-size"); return;
    }
    // A bounded-tranche forecast after route repair, not one oversized swap.
    const leg = min(exported, buckCap);
    if (!leg) { reject("trade-size"); return; }
    const minimum = (netSaleExpected(market, leg) * BigInt(10000 - slippage_bp) / 10000n) * exported / leg;
    if (minimum <= 0n || minimum > a.mortgage) { reject("mortgage-size"); return; }
    const fixed = candidate ? BigInt(Math.trunc(t.fixed_cost * 1e6)) : 0n;
    const pn = state.at_renewal ? 0n : BigInt(Math.ceil(a.p.apr * a.p.penalty_months / 12 * 1e12));
    const penalty = (minimum * pn + 10n ** 12n - 1n) / 10n ** 12n;
    if (cash < a.buffer + maximum + fixed + penalty) { reject("cash-buffer"); return; }
    const coverage = candidate ? candidate.afterCoverage - candidate.beforeCoverage : 0n;
    let fraction = 1, saved = 0n;
    if (candidate && premium) {
      const key = `${candidate.tokenId}:${candidate.coverage}`;
      if (!refunds.has(key)) refunds.set(key, await world.netCreditOps.quotePremiumRefund(candidate.tokenId, candidate.coverage, { blockNumber }));
      const q = refunds.get(key);
      if (q.refundFractionWad === null) { reject("insurer-refund-unavailable"); return; }
      fraction = Number(q.refundFractionWad) / 1e18;
    }
    if (candidate?.coverage) saved = externalPremiumDue(annual, a.face, active) - externalPremiumDue(annual, a.face, active + candidate.coverage);
    const economics = { usd: Number(minimum) / 1e6, wallet_used: Number(wallet) / 1e6, new_spendable: Number(newly) / 1e6,
      present_coverage: Number(coverage) / 1e6, premium: Number(premium) / 1e6, premium_refund_fraction: fraction,
      external_premium_saving: Number(saved) / 1e6, reserve_cost: (Number(maximum) - Number(buy) * t.buck_value) / 1e6,
      reserve_carry: Number(buy) / 1e6 * t.buck_value * (a.p.apr - expected_wallet_growth) };
    if (programme) programmes.push({ candidate, economics, exported });
    else {
      plans.push({ candidate, export_buck: exported, minimum_usdc: minimum, mortgage_limit: minimum,
        cash_buffer: a.buffer, reserve_floor: floor, switch_cost: fixed, penalty_numerator: pn, penalty_denominator: 10n ** 12n,
        reserve_buy_net: buy, reserve_buy_max_usdc: maximum });
      values.push(economics);
    }
  };
  const walletLimit = min(max(0n, book.spendable - retained), buckCap);
  for (const wallet of sizes(walletLimit)) await option(null, wallet);
  const creditCap = min(max(0n, utilization - book.issued), available.sharedLimit), maximumNew = min(creditCap, buckCap);
  const activated = new Map(available.facilities.map(({ tokenId, maxSpendable }) => [String(tokenId), maxSpendable]));
  const issuanceOptions = async (cap, programme = false) => {
    for (const facility of book.facilities) for (const amount of [...new Set([...sizes(cap), min(cap, activated.get(String(facility.tokenId)) ?? 0n)])]
      .filter((v) => v >= RAW).sort((x, y) => x > y ? -1 : x < y ? 1 : 0)) {
      const menu = await world.netCreditOps.issueCoverageMenu(owner, facility.tokenId, amount,
        facility.face * BigInt(facility.premiumRate) * 10n / 10000n, { reserveBuffer: RAW });
      context.issuance_quotes += menu.length;
      for (const candidate of menu) {
        if (candidate.blockNumber !== blockNumber) throw new Error("household assessment block changed");
        if (candidate.reasons.some((r) => r !== "reserve-shortfall")) { candidate.reasons.forEach(reject); continue; }
        if (book.issued + amount + candidate.premium > utilization) { reject("utilization"); continue; }
        const floor = max(retained, reserveFor(candidate));
        const wallet = min(max(0n, book.spendable - floor), max(0n, buckCap - amount));
        for (const spent of programme ? [0n] : [...new Set([0n, wallet])].sort((x, y) => x < y ? -1 : x > y ? 1 : 0)) {
          await option(candidate, spent, programme);
        }
      }
    }
  };
  await issuanceOptions(maximumNew);
  let { index, verdict } = bestQuotedRefinance(t, values);
  if (index === null && plans.length && t.fixed_cost > 0 && buckCap) {
    const minimumLeg = netSaleExpected(market, buckCap) * BigInt(10000 - slippage_bp) / 10000n;
    const programmeCap = minimumLeg ? min(creditCap, a.mortgage * buckCap / minimumLeg) : 0n;
    if (programmeCap > maximumNew) await issuanceOptions(programmeCap, true);
    context.programme_feasible = programmes.length;
    let selected = null;
    for (const [i, plan] of plans.entries()) {
      if (!plan.candidate) continue;
      for (const p of programmes) {
        if (p.candidate.tokenId !== plan.candidate.tokenId) continue;
        const marginal = stagedRefinance(t, values[i], p.economics);
        const better = selected === null || plan.mortgage_limit > selected.usd
          || plan.mortgage_limit === selected.usd && (marginal.verdict.value > selected.value
            || marginal.verdict.value === selected.value && plan.candidate.premium < selected.premium);
        if (marginal.verdict.go && better) {
          index = i; verdict = marginal.verdict;
          selected = { usd: plan.mortgage_limit, value: verdict.value, premium: plan.candidate.premium };
          context.programme = { token_id: p.candidate.tokenId, economics: p.economics, score: marginal.programme,
            tranches: (p.exported + buckCap - 1n) / buckCap, basis: "current-credit/capital/per-tranche-price",
            fixed_cost_paid_now: plan.switch_cost, fixed_cost_allocated: -verdict.parts.fixed };
        }
      }
    }
  }
  if ((await s.client.getBlock()).number !== blockNumber) throw new Error("household assessment block changed");
  context.feasible = plans.length;
  context.selected = index === null ? null : { economics: values[index], score: verdict };
  context.reason = context.programme ? "worthwhile-staged-refinance" : index === null ? "no-worthwhile-refinance" : "worthwhile-refinance";
  return { plan: index === null ? null : plans[index], verdict, context };
}

/** Read-only cash remainder assessment after a proved refusal. No new credit,
 * fund or insurer quote: existing principal/premium/relief stay in the books.
 * Keep original export/principal caps and reserves. The committed closing fee
 * remains in the bank cash budget, rather than being earned/charged a second
 * time in the marginal wallet-export score. */
export async function assessRemainder(a, world, day, { expected_wallet_growth = null, slippage_bp = 50 } = {}) {
  const m = a.refinanceMachine;
  if (!Number.isSafeInteger(day) || day < 0 || world.accounting !== "net" || !m.settlementDue || a.calendarMachine.active) {
    throw new Error("household remainder needs a refused settlement and idle calendar");
  }
  if (a.calendarMachine.bound && a.calendarMachine.day !== day) throw new Error("household calendar books must be prepared before remainder assessment");
  if (!Number.isInteger(slippage_bp) || slippage_bp < 0 || slippage_bp > 1000) throw new Error("invalid household remainder slippage");
  const growth = expected_wallet_growth ?? Math.expm1(-.02);
  if (typeof growth !== "number" || !Number.isFinite(growth) || growth < -1) {
    throw new Error("invalid expected wallet growth");
  }
  const flow = m.flow, phase = flow.phase, revision = flow.revision ?? 0, origin = flow.origin ?? flow.plan;
  const s = world.session, owner = world.holderAddress(a.account), block = (await s.client.getBlock()).number;
  const at = (c, functionName, args = []) => s.client.readContract({ address: c.address, abi: c.abi, functionName, args, blockNumber: block });
  const principal = min(origin.mortgage_limit, a.mortgage), cash = await at(world.usdc, "balanceOf", [owner]);
  const closing = a.refinanceClosed ? 0n : origin.switch_cost;
  const base = { ...flow.plan, candidate: null, mortgage_limit: principal, switch_cost: closing,
    cash_buffer: max(origin.cash_buffer, a.buffer), reserve_floor: origin.reserve_floor,
    penalty_numerator: origin.penalty_numerator, penalty_denominator: origin.penalty_denominator,
    reserve_buy_net: 0n, reserve_buy_max_usdc: 0n };
  const context = { block, phase, revision, usdc: cash, mortgage: a.mortgage, export_cap: origin.export_buck,
    principal_cap: origin.mortgage_limit, reserve_floor: base.reserve_floor, cash_buffer: base.cash_buffer,
    committed_closing_usdc: closing, premium_already_paid: origin.candidate?.premium ?? 0n, new_credit: false };
  let plan = null, nextPhase = phase;
  if (phase === "mortgage" || !principal) {
    nextPhase = "mortgage";
    const bank = bankTerms(base);
    Object.assign(context, { basis: "existing-exported-usdc", bank,
      reason: !bank.amount || cash >= base.cash_buffer + bank.amount ? "ready" : "cash-buffer" });
    if (context.reason === "ready") plan = base;
  } else {
    if (!await at(world.reg, "isVerified", [owner]) || await at(world.reg, "isCarrying", [owner])) {
      throw new Error("household remainder needs a verified ordinary wallet");
    }
    const held = await at(world.buck, "balanceOf", [owner]);
    const market = await observeNetMarket(s, world.buck, world.usdc, world.poolUB, world.fees.ub, { blockNumber: block });
    const fair = Number(await at(world.basket, "basketValueInBuck")) / 1e18 * (Number(market.price_num) / Number(market.price_den));
    const t = { debt: Number(a.mortgage) / 1e6, rate: a.p.apr, face: Number(a.face) / 1e6,
      premium_bp: a.p.premium_bp, joined: true, payback_y: a.p.theta, risk: a.p.risk,
      headroom: Number(held) / 1e6, buck_value: fair, penalty_months: 0, fixed_cost: 0,
      expected_relief_rate: 0, expected_refund_growth: 0, expected_wallet_growth: growth,
      premium_refund_fraction: 1, wallet_already_issued: true, issuance_limit: 0 };
    // Same owned 0.1% sizing cushion as the existing trade policies; it is
    // neither another demurrage calculation nor a paid trading fee.
    const available = max(0n, held - base.reserve_floor - (held + 999n) / 1000n);
    let cap = min(origin.export_buck, available, market.reserve_buck * BigInt(Math.trunc(a.p.max_impact_bp * 50)) / 1_000_000n);
    const target = (principal * 10000n + BigInt(9999 - slippage_bp)) / BigInt(10000 - slippage_bp);
    const needed = target <= MAX ? netSaleInput(market, target) : null;
    if (needed !== null) cap = min(cap, needed);
    Object.assign(context, { basis: "existing-buck/marginal", held_buck: held, available_buck: available,
      export_budget: cap, expected_wallet_growth: growth, rejections: {}, reason: cap ? "no-worthwhile-remainder" : "export-budget" });
    let selected = null;
    for (const amount of sizes(cap)) {
      const minimum = netSaleExpected(market, amount) * BigInt(10000 - slippage_bp) / 10000n, retired = min(principal, minimum);
      if (!amount || !retired) continue;
      const candidate = { ...base, export_buck: amount, minimum_usdc: minimum, mortgage_limit: retired }, bank = bankTerms(candidate);
      if (cash + minimum < candidate.cash_buffer + bank.amount) {
        context.rejections["cash-buffer"] = (context.rejections["cash-buffer"] ?? 0) + 1; continue;
      }
      const v = refinanceQuoted(t, Number(retired) / 1e6, Number(amount) / 1e6, 0, 0, 0), penalty = Number(bank.penalty) / 1e6;
      v.parts.penalty = -penalty; v.one_time += penalty; v.value -= penalty; v.go = v.go && v.value > 0;
      if (!v.go) context.rejections.economics = (context.rejections.economics ?? 0) + 1;
      const better = selected === null || retired > selected.principal
        || retired === selected.principal && (v.value > selected.value || v.value === selected.value && amount < selected.amount);
      if (v.go && better) {
        plan = candidate; selected = { principal: retired, value: v.value, amount };
        Object.assign(context, { reason: "ready", score: v, bank });
      }
    }
  }
  if ((await s.client.getBlock()).number !== block || m.flow !== flow || !m.settlementDue || a.calendarMachine.active
      || flow.phase !== phase || (flow.revision ?? 0) !== revision) throw new Error("household remainder observation changed");
  return { plan, phase: nextPhase, context };
}

/** One serialized local-EVM operation and append-only public event stream.
 * It is neither an economic optimizer nor a complete saved-world importer.
 * The household owns its calendar and counterfactual; receipt evidence does
 * not establish chain finality. All event money uses decimal strings. */
export class HouseholdRefinance {
  constructor(agent) { this.agent = agent; this.flow = null; this.events = []; this.decisions = 0; }
  get active() { return this.flow !== null && ["running", "pending"].includes(this.flow.status); }
  get settlementDue() { return this.flow !== null && this.flow.status === "refused" && ["export", "mortgage"].includes(this.flow.phase); }
  get paymentDue() { return this.settlementDue && this.flow.phase === "mortgage"; }
  status() {
    const f = this.flow;
    return { schema: 1, decision: this.decisions, status: f?.status ?? "idle", phase: f?.phase ?? null,
      hash: f?.hash ?? null, payment_due: this.paymentDue, settlement_due: this.settlementDue,
      revision: f?.revision ?? 0, events: this.events.length };
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
        const [face, active] = await at(world.credit, "creditInfo", [id]);
        const deposit = await at(world.buck, "premiumPrincipal", [id]);
        facilities.push({ token_id: id, issued: principal, claim_seconds: seconds, face, activated: active, premium_nominal: deposit });
        issued += principal; premium += deposit;
      }
      const limit = await at(world.buck, "creditLimit", [owner]);
      const monthlyPremium = a.sold ? 0n : a.face * BigInt(a.p.premium_bp) / 10000n / 12n;
      const externalPremium = externalPremiumDue(monthlyPremium, a.face, facilities.reduce((sum, f) => sum + f.activated, 0n));
      const buffer = this.flow?.plan.cash_buffer ?? a.buffer, reserve = this.flow?.plan.reserve_floor ?? 0n, custody = {};
      for (const [name, proxy] of [["savings", a.savings], ["wallet", a.wallet]]) if (proxy) {
        custody[name] = { usdc: await at(world.usdc, "balanceOf", [proxy.address]),
          buck_scaled: await at(world.buck, "netBalanceScaled", [proxy.address]) };
      }
      return wire({ usdc: cash, buck: bucks, buck_scaled: await at(world.buck, "netBalanceScaled", [owner]),
        issued, credit_limit: limit, custody, facilities, premium_nominal: premium, mortgage: a.mortgage, external_paid: a.externalPaid,
        external_interest: a.externalInterest, income_total: a.incomeTotal, living_paid: a.livingPaid,
        external_premium_paid: a.extPremium, living_arrears: a.livingArrears, premium_arrears: a.premiumArrears,
        counterfactual_cash: a.hypoCash, counterfactual_mortgage: a.hypoMortgage,
        counterfactual_interest: a.hypoInterest, counterfactual_paid: a.hypoPaid, calendar: a.calendarMachine.status(),
        premium_paid: a.premiumPaid, switch_paid: a.switchPaid, penalty_paid: a.penaltyPaid, refinance_closed: a.refinanceClosed,
        external_premium_monthly: externalPremium, counterfactual_premium_monthly: monthlyPremium,
        cash_buffer: buffer, reserve_floor: reserve, ratios: { utilization: limit ? Number(issued) / Number(limit) : null,
          cash_buffer_cover: buffer ? Number(cash) / Number(buffer) : null, reserve_cover: reserve ? Number(bucks) / Number(reserve) : null } });
    } catch (err) { return { unavailable: String(err).slice(0, 200) }; }
  }
  async market(world) {
    try {
      const s = world.session, blockNumber = (await s.client.getBlock()).number;
      const at = (c, functionName, args = []) => s.client.readContract({ address: c.address, abi: c.abi, functionName, args, blockNumber });
      const quote = await observeNetMarket(s, world.buck, world.usdc, world.poolUB, world.fees.ub, { blockNumber });
      return wire({ block: blockNumber, k_wad: await at(world.kctrl, "currentBuckK"),
        bvib_wad: await at(world.basket, "basketValueInBuck"),
        nominal_usdc_per_buck: { numerator: quote.price_num, denominator: quote.price_den },
        active_reserves: { usdc: quote.reserve_usdc, buck: quote.reserve_buck },
        pool_nominal_scaled: quote.gross_scaled, pool_net_scaled: quote.net_scaled, fee_ppm: BigInt(world.fees.ub) });
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
      day, block, phase: this.flow?.phase ?? "idle", status, accounts: await this.accounts(world), ...wire(detail) });
  }
  async start(world, given, day, ctr = {}, { context = {} } = {}) {
    if (this.active) throw new Error("household refinancing already active");
    if (this.paymentDue) throw new Error("household bank payment due; retry before a new refinance");
    if (this.settlementDue) throw new Error("household has an unsettled export; resolve before a new refinance");
    const p = { switch_cost: 0n, penalty_numerator: 0n, penalty_denominator: 1n,
      reserve_buy_net: 0n, reserve_buy_max_usdc: 0n, ...given };
    const values = [p.export_buck, p.minimum_usdc, p.mortgage_limit, p.cash_buffer, p.reserve_floor,
      p.switch_cost, p.penalty_numerator, p.penalty_denominator, p.reserve_buy_net, p.reserve_buy_max_usdc];
    if (values.some((v) => typeof v !== "bigint" || v < 0n || v > MAX)
        || [p.export_buck, p.minimum_usdc, p.mortgage_limit, p.penalty_denominator].some((v) => v <= 0n)
        || p.mortgage_limit > p.minimum_usdc || p.mortgage_limit > this.agent.mortgage
        || p.cash_buffer < this.agent.buffer) {
      throw new Error("invalid household refinance bounds");
    }
    const a = this.agent, owner = world.holderAddress(a.account), ids = world._credits.get(a.account.address) ?? [];
    const c = p.candidate, funding = p.reserve_buy_net > 0n;
    if (Boolean(p.reserve_buy_max_usdc) !== funding || funding && !c) throw new Error("invalid household reserve funding");
    if (c && (!Array.isArray(c.reasons) || c.reasons.some((reason) => reason !== "reserve-shortfall" || !funding)
        || !ids.includes(c.tokenId))) {
      throw new Error("unavailable household issuance candidate");
    }
    if (c && p.reserve_buy_net < c.reserveShortfall) throw new Error("refinance reserve purchase is insufficient");
    if (c && p.reserve_floor < reserveFor(c)) throw new Error("refinance must retain the quoted BUCK reserve");
    if (!c && p.switch_cost) throw new Error("wallet prepayment cannot switch insurance");
    if (a.refinanceClosed && p.switch_cost) throw new Error("household refinance closing was already paid");
    const book = await world.netCreditOps.household(owner, ids);
    for (const id of ids) if ((await world.session.call(world.buck, "reductionAuthorization", [id]))[0]) {
      throw new Error("household reduction authorization pending");
    }
    if (book.spendable + (c?.requestedSpendable ?? 0n) + p.reserve_buy_net < p.export_buck + p.reserve_floor) {
      throw new Error("refinance would consume the BUCK reserve");
    }
    const bank = bankTerms(p);
    if (bank.amount > MAX) throw new Error("household bank allocation overflows uint256");
    const cash = await world.session.call(world.usdc, "balanceOf", [owner]);
    if (funding && cash < p.cash_buffer + p.reserve_buy_max_usdc) throw new Error("reserve purchase would consume the USDC buffer");
    if (cash + p.minimum_usdc < p.cash_buffer + p.reserve_buy_max_usdc + bank.amount) {
      throw new Error("refinance would consume the USDC buffer");
    }
    context = wire(context);
    this.decisions++;
    this.counters = ctr;
    this.flow = { plan: { ...p, candidate: c ? { ...c, reasons: [...c.reasons] } : null },
      phase: funding ? "fund" : c ? "issue" : "export", status: "running", hash: null, issue_candidate: null,
      initial_closed: a.refinanceClosed, terms: null, revision: 0 };
    this.flow.origin = this.flow.plan;
    a.fundingPending = true;
    const limits = Object.fromEntries(["surplus_policy", "theta", "issue_premium", "retire_disc", "utilization", "max_impact_bp"]
      .map((k) => [k, a.p[k]]));
    const plan = { ...this.flow.plan, candidate: c ? Object.fromEntries(Object.entries(c).map(([k, v]) => [snake(k), v])) : null };
    await this.event(world, day, "planned", { plan, context, limits, market: await this.market(world), basket: await this.basket(world) });
    return this.advance(world, day, ctr);
  }
  /** Compatibility bank-only entry; reuse the shared remainder assessment. */
  async retryPayment(world, day, ctr = this.counters ?? {}) {
    if (!this.paymentDue) throw new Error("household has no refused bank payment to retry");
    return this.resume(world, day, ctr);
  }
  /** Only known refused cash phases can resume. Keep original caps and all
   * confirmed steps; authenticate recovery against the new saved bounds.
   * A declined quote sends nothing, and an unknown outcome cannot reprice. */
  async resume(world, day, ctr = this.counters ?? {}, options = {}) {
    const source = this.flow;
    const assessed = await assessRemainder(this.agent, world, day, options);
    if (this.flow !== source || !this.settlementDue || this.agent.calendarMachine.active
        || source.phase !== assessed.context.phase || (source.revision ?? 0) !== assessed.context.revision) {
      throw new Error("household remainder observation changed");
    }
    this.agent.fundingPending = true;
    if (assessed.plan === null) {
      this.agent.lastAction = "refinance-wait"; this.agent.lastError = assessed.context.reason;
      await this.event(world, day, "waiting", { context: assessed.context }); return true;
    }
    const previous = this.flow.plan;
    Object.assign(this.flow, { plan: assessed.plan, phase: assessed.phase, status: "running", hash: null,
      terms: null, revision: (this.flow.revision ?? 0) + 1 });
    await this.event(world, day, "resumed", { context: assessed.context, prior_plan: previous,
      plan: assessed.plan, revision: this.flow.revision });
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
    if (phase === "fund") {
      const p = f.plan;
      if (result.paid <= 0n || result.paid > p.reserve_buy_max_usdc || result.received < p.reserve_buy_net) {
        throw new Error("household reserve purchase does not match bounds");
      }
      a.tradeLoss += result.paid - result.received;
      amounts = { usdc: result.paid, net_buck_scaled: result.receivedScaled, nominal_buck: result.nominalReceived };
    } else if (phase === "issue") {
      a.premiumPaid += result.premium; a.joined = true;
      amounts = { principal: result.principal, premium: result.premium, spendable: result.spendable };
    } else if (phase === "export") {
      a.tradeLoss += result.paid - result.received;
      ctr.rtrBuckSold = (ctr.rtrBuckSold ?? 0n) + result.paid / RAW;
      amounts = { buck: result.paid, nominal_buck: result.nominalPaid, usdc: result.received };
    } else {
      const t = f.terms;
      if (result.amount !== t.amount || a.mortgage < t.principal) throw new Error("household bank allocation changed");
      if (a.calendarMachine.bound && t.principal) await a.calendarMachine.recordMortgage(world, receipt, t.principal);
      else if (t.principal) { a.mortgage -= t.principal; a.externalPaid += t.principal; }
      a.switchPaid += t.switch; a.penaltyPaid += t.penalty;
      if ((f.origin ?? f.plan).candidate) a.refinanceClosed = true;
      if (t.principal) {
        a.refis++; a.deploys++;
        const key = f.initial_closed ? "rtrTopUps" : "rtrRefis";
        ctr[key] = (ctr[key] ?? 0) + 1;
        ctr.rtrUsdRetired = (ctr.rtrUsdRetired ?? 0n) + t.principal / RAW;
      }
      amounts = { usdc: result.amount, principal_usdc: t.principal, switch_usdc: t.switch, penalty_usdc: t.penalty };
    }
    // Commit financial effects/cursor before optional account observations.
    f.hash = null; f.terms = null; f.status = "running";
    f.phase = phase === "mortgage" ? "complete" : PHASES[PHASES.indexOf(phase) + 1];
    await this.event(world, day, "confirmed", { operation: phase, amounts,
      transaction_hash: receipt.transactionHash, receipt_block: receipt.blockNumber ?? null });
  }
  async applyKnown(world, day, result, ctr) {
    const f = this.flow, phase = f.phase;
    f.hash = result.receipt?.transactionHash ?? result.transactionHash;
    try { await this.apply(world, day, result, ctr); return true; }
    catch (err) {
      if (f.phase !== phase) throw err;
      f.status = "pending"; this.agent.lastAction = "refinance-pending";
      this.agent.lastError = `refinance accounting unavailable: ${err}`.slice(0, 200);
      await this.event(world, day, "pending", { reason: this.agent.lastError, transaction_hash: f.hash }); return false;
    }
  }
  async recover(world) {
    const f = this.flow, a = this.agent, owner = world.holderAddress(a.account), p = f.plan;
    const receipt = await world.session.client.getTransactionReceipt({ hash: f.hash });
    if (receipt?.transactionHash?.toLowerCase() !== f.hash.toLowerCase()) throw new Error("household receipt hash mismatch");
    if (receipt.status === "reverted") throw new TransactionReverted(f.phase, "household operation reverted", receipt);
    if (f.phase === "fund") return netPurchaseFromReceipt(world, world.netMarket, { address: owner }, receipt, f.hash);
    if (f.phase === "issue") return world.netCreditOps.issuanceFromReceipt(owner, f.issue_candidate, receipt, f.hash);
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
      if (!await this.applyKnown(world, day, result, ctr)) return true;
    }
    while (f.phase !== "complete") {
      const p = f.plan, phase = f.phase;
      if (phase === "issue") {
        // A confirmed reserve buy stays committed while an unavailable view
        // is retried. Neither that retry nor a changed quote buys twice.
        const c = p.candidate;
        let fresh;
        try { fresh = await world.netCreditOps.assessIssueCandidate(owner, c.tokenId, c.coverage,
          c.requestedSpendable, c.maxPremium, { reserveBuffer: c.reserveBuffer }); }
        catch (err) {
          a.lastError = `refinance issuance observation unavailable: ${err}`.slice(0, 200);
          await this.event(world, day, "waiting", { reason: a.lastError }); return true;
        }
        if (fresh.reasons.length || ["priorPrincipal", "beforeCoverage", "afterCoverage", "premium"].some((key) => fresh[key] !== c[key])
            || fresh.premiumPool.toLowerCase() !== c.premiumPool.toLowerCase()
            || reserveFor(fresh) > p.reserve_floor) {
          await this.refuse(world, day, "refinance issuance terms changed; reassess retained holdings"); return true;
        }
        f.issue_candidate = fresh;
      } else if (phase === "fund") {
        if (await s.call(world.usdc, "balanceOf", [owner]) < p.reserve_buy_max_usdc + p.cash_buffer) {
          await this.refuse(world, day, "refinance reserve USDC buffer changed"); return true;
        }
      } else if (phase === "mortgage") {
        const bank = bankTerms(p);
        if (!bank.amount) {
          f.phase = "complete"; f.satisfied = true;
          await this.event(world, day, "satisfied", { reason: "mortgage already retired; no bank amount due" }); continue;
        }
        if (await s.call(world.usdc, "balanceOf", [owner]) < bank.amount + p.cash_buffer) {
          await this.refuse(world, day, "refinance USDC buffer changed"); return true;
        }
        f.terms = bank;
      } else if (phase === "export" && await s.call(world.buck, "balanceOf", [owner]) < p.export_buck + p.reserve_floor) {
        await this.refuse(world, day, "refinance BUCK reserve changed"); return true;
      }
      f.status = "pending"; f.hash = null;
      let result;
      try {
        if (phase === "fund") result = await buyBuckExact(world, world.netMarket, { address: owner },
          p.reserve_buy_net, p.reserve_buy_max_usdc);
        else if (phase === "issue") result = await world.netCreditOps.issueCandidate(owner, f.issue_candidate);
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
      if (!await this.applyKnown(world, day, result, ctr)) return true;
    }
    f.status = "complete"; a.fundingPending = false; a.lastAction = f.satisfied ? "refinance-satisfied" : "refinance"; a.lastError = "";
    await this.event(world, day, "complete");
    return true;
  }
}
