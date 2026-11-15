// Checkpointed canonical refinancing; mirrored by sim/net_household.py.
// The caller supplies an assessed economic plan and optional reserve purchase. Existing SDKs enforce
// issuance/trade/payment bounds and authenticate each receipt. Only the exact
// bank payment changes the mortgage; an uncertain phase never sends twice.
import { IssuancePending, ReleasePending } from "../net-world.js";
import { buyBuck, buyBuckExact, netPurchaseFromReceipt, sellBuck, netSaleFromReceipt } from "../market.js";
import { TransactionReceiptPending, TransactionReverted } from "../session.js";
import { markNetBasketPositions, netBasketOps } from "../basket.js";
import { observeNetMarket, netPurchaseCost, netPurchaseExpected, netSaleExpected, netSaleInput } from "../net-market-quote.js";
import { externalPremiumDue, terms, bestQuotedRefinance, stagedRefinance, refinanceQuoted, netSurplusPurchase,
  netWalletAdjustment, netWalletTarget, investmentWant, investmentProgramme, investmentQuoted, stagedInvestment,
  netInvestorAction, NO } from "./household-policy.js";

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

// Existing 0.1% owned trade cushion; not another demurrage calculation.
// The actual wallet balance and unchanged reserve guard govern execution.
// The BUCK's fair dollar value: USDC per net BUCK at parity, as { num, den, basis } (Python
// net_household.fair_usdc). Owner, 2026-11-15: a holding is worth what selling it fetches. While the
// price book (BasketReference) is fresh, its basket in USDC by each TOKEN's best route; a world
// without a book reads the basket in net BUCK at the dollar pool's price. `at` reads at the
// household's pinned block; `q` is that block's market.
export const fairUsdc = async (world, at, q) => {
  const book = world.machine?.parity;
  if (book && await at(book, "fresh")) {
    const value = await at(book, "valueUsdc");
    if (value) return { num: value, den: 10n ** 18n, basis: "the book's basket in USDC" };
  }
  return { num: await at(world.basket, "basketValueInBuck") * q.price_num, den: 10n ** 18n * q.price_den,
    basis: "basket-in-buck x dollar-pool spot" };
};
export const exportable = (held, reserve) => max(0n, held - reserve - (held + 999n) / 1000n);
export const basketOps = (world) => netBasketOps(world.session, world.basket, world.buck,
  { execute: world.netMarket.execute, simulate: world.netMarket.simulate });
export const EXIT_STEPS = ["redeem", "sell", "release"];
const later = (x, y) => { for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i] > y[i]; return false; };
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
  // The lasting issuance front, not the last operation's cursor: a surplus
  // flow temporarily protects ALL prior wallet BUCK, and that must not freeze
  // owned cash out of a later mortgage conversion.
  reserve_floor ??= a.netCreditReserve;
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
  const fair = await fairUsdc(world, at, market), month = Math.floor((day - a.arrive_day) / 30);
  const state = { accounting: "net", mortgage: Number(a.mortgage) / 1e6, face: Number(a.face) / 1e6,
    joined: a.joined, closing_paid: a.refinanceClosed, k: Number(k) / 1e18, spendable: Number(book.spendable) / 1e6,
    unactivated: available.maturitiesPending ? 0 : Number(available.unactivatedPresent) / 1e6,
    activated_capacity: Number(available.capacity) / 1e6,
    issuance_limit: available.maturitiesPending ? 0 : Number(available.sharedLimit) / 1e6,
    at_renewal: (month - a.p.renew_in) % a.p.term_months === 0,
    basket_usd: Number(fair.num) / Number(fair.den),
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
  const plans = [], values = [], programmes = [], unfunded = [], funding = [], refunds = new Map(), active = book.facilities.reduce((n, f) => n + f.activated, 0n);
  const annual = a.face * BigInt(a.p.premium_bp) / 10000n;
  const reject = (reason) => { context.rejections[reason] = (context.rejections[reason] ?? 0) + 1; };
  const option = async (candidate, wallet, programme = false) => {
    context.considered++;
    const newly = candidate?.requestedSpendable ?? 0n, premium = candidate?.premium ?? 0n;
    const floor = candidate ? max(retained, reserveFor(candidate)) : retained;
    const buy = candidate ? max(0n, floor - book.spendable) : 0n, cost = netPurchaseCost(market, buy);
    if (cost === null) { reject("reserve-liquidity"); return; }
    const maximum = (cost * BigInt(10000 + slippage_bp) + 9999n) / 10000n;
    // Funding/issuance and export are distinct blocks. Keep an owned cushion
    // rather than requiring the exact quoted net balance to survive them.
    const projected = book.spendable + newly + buy, exported = min(newly + wallet, exportable(projected, floor));
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
    const cashFront = a.buffer + maximum + fixed + penalty;
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
    const economics = { usd: Number(minimum) / 1e6, wallet_used: Number(max(0n, exported - newly)) / 1e6, new_spendable: Number(newly) / 1e6,
      present_coverage: Number(coverage) / 1e6, premium: Number(premium) / 1e6, premium_refund_fraction: fraction,
      external_premium_saving: Number(saved) / 1e6, reserve_cost: (Number(maximum) - Number(buy) * t.buck_value) / 1e6,
      reserve_carry: Number(buy) / 1e6 * t.buck_value * (a.p.apr - expected_wallet_growth) };
    if (programme) programmes.push({ candidate, economics, exported, cashFront });
    else {
      const plan = { candidate, export_buck: exported, minimum_usdc: minimum, mortgage_limit: minimum,
        cash_buffer: a.buffer, reserve_floor: floor, switch_cost: fixed, penalty_numerator: pn, penalty_denominator: 10n ** 12n,
        reserve_buy_net: buy, reserve_buy_max_usdc: maximum };
      if (cash < cashFront) {
        reject("cash-buffer"); unfunded.push({ plan, economics, cashFront });
        if (bestQuotedRefinance(t, [economics]).verdict.go) funding.push({ cashFront, basis: "worthwhile-leg" });
      } else { plans.push(plan); values.push(economics); }
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
  if (index === null && (plans.length || unfunded.length) && t.fixed_cost > 0 && buckCap) {
    const minimumLeg = netSaleExpected(market, buckCap) * BigInt(10000 - slippage_bp) / 10000n;
    const programmeCap = minimumLeg ? min(creditCap, a.mortgage * buckCap / minimumLeg) : 0n;
    if (programmeCap > maximumNew) await issuanceOptions(programmeCap, true);
    context.programme_quotes = programmes.length;
    context.programme_feasible = programmes.filter((p) => p.cashFront <= cash).length;
    let selected = null;
    const legs = plans.map((plan, i) => ({ i, plan, economics: values[i],
      cashFront: plan.cash_buffer + plan.reserve_buy_max_usdc + plan.switch_cost + bankTerms(plan).penalty }))
      .concat(unfunded.map((p) => ({ ...p, i: null })));
    for (const { i, plan, economics, cashFront } of legs) {
      if (!plan.candidate) continue;
      for (const p of programmes) {
        if (p.candidate.tokenId !== plan.candidate.tokenId) continue;
        const marginal = stagedRefinance(t, economics, p.economics);
        if (marginal.verdict.go && max(cashFront, p.cashFront) > cash) {
          funding.push({ cashFront: max(cashFront, p.cashFront), basis: "worthwhile-programme" });
        }
        if (i === null || p.cashFront > cash) continue; // targets do not authorize an unfunded loan
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
  if (index !== null) {
    const p = plans[index], retainedCash = book.spendable + (p.candidate?.requestedSpendable ?? 0n) + p.reserve_buy_net - p.export_buck;
    Object.assign(context.selected, { post_export_buck_quote: retainedCash, export_cushion_buck: retainedCash - p.reserve_floor });
  }
  const p = index === null ? null : plans[index];
  const hold = p ? { cashFront: p.cash_buffer + p.reserve_buy_max_usdc + p.switch_cost + bankTerms(p).penalty, basis: "selected-leg" }
    : funding.sort((x, y) => x.cashFront < y.cashFront ? -1 : x.cashFront > y.cashFront ? 1 : x.basis.localeCompare(y.basis))[0]
      ?? { cashFront: a.buffer, basis: "no-worthwhile-credit" };
  context.funding = { cash_floor: hold.cashFront, basis: hold.basis, unfunded: hold.cashFront > cash, refinance_ready: index !== null };
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
    const value = await fairUsdc(world, at, market), fair = Number(value.num) / Number(value.den);
    const t = { debt: Number(a.mortgage) / 1e6, rate: a.p.apr, face: Number(a.face) / 1e6,
      premium_bp: a.p.premium_bp, joined: true, payback_y: a.p.theta, risk: a.p.risk,
      headroom: Number(held) / 1e6, buck_value: fair, penalty_months: 0, fixed_cost: 0,
      expected_relief_rate: 0, expected_refund_growth: 0, expected_wallet_growth: growth,
      premium_refund_fraction: 1, wallet_already_issued: true, issuance_limit: 0 };
    // Same owned 0.1% sizing cushion as the existing trade policies; it is
    // neither another demurrage calculation nor a paid trading fee.
    const available = exportable(held, base.reserve_floor);
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
// Read-only successor for surplus, using the same pinned money graph/menu.
// Existing wallet cash and worthwhile credit funding stay protected. Actual
// basket exit simulation uses eth_call; it never bypasses K or mutates the chain.
/** Read-only choice of one quoted issue a mortgage-free household keeps and invests
 * (net_household.assess_investment). The amount is investmentWant; each bounded actual NFT quote is
 * scored by investmentQuoted with the dollars its new BUCK would fetch at this block's pool. Wallet
 * money is never capacity; the lasting reserve stays; a reserve shortfall is a guarded purchase; the
 * closing fee is fronted in dollars. A first bite is scored against the whole carry line. */
export async function assessInvestment(a, world, day, { expected_relief_rate, expected_refund_growth,
  expected_wallet_growth, reserve_floor = null, slippage_bp = 50 } = {}) {
  const m = a.refinanceMachine, ids = world._credits.get(a.account.address) ?? [];
  if (world.accounting !== "net" || m.active || m.settlementDue || m.surplusDue || a.calendarMachine.active) {
    throw new Error("household investment assessment needs idle canonical accounting");
  }
  if (a.investor || a.mortgage || a.sold || !ids.length) throw new Error("credit investment needs a mortgage-free insured household");
  if (a.netRepaymentTarget !== null || a.netTradeUncertain || a.netCancelUncertain || a.netPendingToken !== null
      || a.netRepaymentInterrupted) throw new Error("household repayment must be reconciled before credit investment");
  reserve_floor ??= a.netCreditReserve;
  if (typeof reserve_floor !== "bigint" || reserve_floor < 0n || reserve_floor > MAX
      || !Number.isInteger(slippage_bp) || slippage_bp < 0 || slippage_bp > 1000) throw new Error("invalid household assessment reserve or slippage");
  const s = world.session, owner = world.holderAddress(a.account), blockNumber = (await s.client.getBlock()).number;
  const at = (c, functionName, args = []) => s.client.readContract({ address: c.address, abi: c.abi, functionName, args, blockNumber });
  for (const id of ids) if ((await at(world.buck, "reductionAuthorization", [id]))[0]) throw new Error("household repayment authorization remains pending");
  const market = await observeNetMarket(s, world.buck, world.usdc, world.poolUB, world.fees.ub, { blockNumber });
  const book = await world.netCreditOps.household(owner, ids, { blockNumber });
  const retained = min(reserve_floor, book.spendable);
  const available = await world.netCreditOps.quoteHouseholdActivatedCapacity(owner, ids);
  if (available.blockNumber !== blockNumber) throw new Error("household assessment block changed");
  const cash = await at(world.usdc, "balanceOf", [owner]), k = await at(world.kctrl, "currentBuckK");
  const fair = await fairUsdc(world, at, market);
  // spendable is zero: wallet BUCK is money already owned, not issuing capacity.
  const state = { accounting: "net", mortgage: 0, face: Number(a.face) / 1e6, joined: a.joined,
    closing_paid: a.refinanceClosed, k: Number(k) / 1e18, spendable: 0, drawn: Number(book.issued) / 1e6,
    unactivated: available.maturitiesPending ? 0 : Number(available.unactivatedPresent) / 1e6,
    activated_capacity: Number(available.capacity) / 1e6,
    issuance_limit: available.maturitiesPending ? 0 : Number(available.sharedLimit) / 1e6, at_renewal: false,
    ru: Number(market.reserve_usdc) / 1e6, rb: Number(market.reserve_buck) / 1e6, fee: Number(market.fee_ppm) / 1e6,
    basket_usd: Number(fair.num) / Number(fair.den),
    expected_relief_rate, expected_refund_growth, expected_wallet_growth, premium_refund_fraction: 1 };
  const context = { block: blockNumber, considered: 0, issuance_quotes: 0, rejections: {}, feasible: 0, cash,
    forecasts: { relief_rate: expected_relief_rate, refund_growth: expected_refund_growth, wallet_growth: expected_wallet_growth },
    protected_buck: reserve_floor };
  const done = async (plan, verdict, reason) => {
    if ((await s.client.getBlock()).number !== blockNumber) throw new Error("household assessment block changed");
    context.reason = reason;
    return { plan, verdict, context };
  };
  if (!market.reserve_buck || !market.reserve_usdc || !fair.num) return done(null, NO, "market-unavailable");
  const [wanted, why] = investmentWant(state, a.p), want = BigInt(Math.trunc(wanted * 1e6));
  Object.assign(context, { want, basis: why });
  if (want < RAW) return done(null, NO, why === "hold" ? "hold" : "below-issue-grain");
  const t = terms(state, a.p, true);
  const utilization = a.face * k * BigInt(Math.trunc(a.p.utilization * 1e9)) / (10n ** 18n * 10n ** 9n);
  const impactPpm = BigInt(Math.trunc(a.p.max_impact_bp * 50));
  const buckCap = market.reserve_buck * impactPpm / 1_000_000n, cashCap = market.reserve_usdc * impactPpm / 1_000_000n;
  const fixed = BigInt(Math.trunc(t.fixed_cost * 1e6));
  const active = book.facilities.reduce((n, f) => n + f.activated, 0n), annual = a.face * BigInt(a.p.premium_bp) / 10000n;
  let position = 0n;
  for (const existing of a.receipts) {
    if ((await at(world.basket, "deposits", [existing]))[2].toLowerCase() === world.buck.address.toLowerCase()) { position = existing; break; }
  }
  const refunds = new Map(), bites = [];
  let best = null;
  const reject = (reason) => { context.rejections[reason] = (context.rejections[reason] ?? 0) + 1; return null; };
  // One actual candidate's plan and economics, or null with its rejection counted.
  const quoted = async (candidate, programme = false) => {
    context.considered++;
    if (candidate.reasons.some((r) => r !== "reserve-shortfall")) { candidate.reasons.forEach(reject); return null; }
    const newly = candidate.requestedSpendable, premium = candidate.premium;
    if (book.issued + newly + premium > utilization) return reject("utilization");
    const floor = max(retained, reserveFor(candidate)), buy = max(0n, floor - book.spendable);
    const cost = netPurchaseCost(market, buy);
    if (cost === null) return reject("reserve-liquidity");
    const maximum = (cost * BigInt(10000 + slippage_bp) + 9999n) / 10000n;
    // A programme is a forecast of bounded bites, not one large issue or reserve purchase.
    if (!programme && (newly > buckCap || maximum > cashCap || buy * market.gross_scaled > buckCap * market.net_scaled)) {
      return reject("trade-size");
    }
    const deposit = min(newly, exportable(book.spendable + newly + buy, floor));
    if (deposit < RAW) return reject("deposit-size");
    if (cash < a.buffer + maximum + fixed) return reject("cash-buffer");
    let fraction = 1, saved = 0n;
    if (premium) {
      const key = `${candidate.tokenId}:${candidate.coverage}`;
      if (!refunds.has(key)) refunds.set(key, await world.netCreditOps.quotePremiumRefund(candidate.tokenId, candidate.coverage, { blockNumber }));
      if (refunds.get(key).refundFractionWad === null) return reject("insurer-refund-unavailable");
      fraction = Number(refunds.get(key).refundFractionWad) / 1e18;
    }
    if (candidate.coverage) saved = externalPremiumDue(annual, a.face, active) - externalPremiumDue(annual, a.face, active + candidate.coverage);
    // What the new BUCK would fetch at this pool, a bite at a time.
    const leg = min(newly, buckCap);
    if (!leg) return reject("trade-size");
    const economics = { usd: Number(netSaleExpected(market, leg) * newly / leg) / 1e6, new_spendable: Number(newly) / 1e6,
      present_coverage: Number(candidate.afterCoverage - candidate.beforeCoverage) / 1e6, premium: Number(premium) / 1e6,
      premium_refund_fraction: fraction, external_premium_saving: Number(saved) / 1e6,
      reserve_cost: (Number(maximum) - Number(buy) * t.buck_value) / 1e6,
      reserve_carry: Number(buy) / 1e6 * t.buck_value * (a.p.apr - expected_wallet_growth) };
    return { plan: { candidate, deposit_buck: deposit, cash_buffer: a.buffer, reserve_floor: floor, switch_cost: fixed,
      reserve_buy_net: buy, reserve_buy_max_usdc: maximum, position_id: position, slippage_bp }, economics };
  };
  const menu = async (facility, amount) => {
    const found = await world.netCreditOps.issueCoverageMenu(owner, facility.tokenId, amount,
      facility.face * BigInt(facility.premiumRate) * 10n / 10000n, { reserveBuffer: RAW });
    context.issuance_quotes += found.length;
    if (found.some((c) => c.blockNumber !== blockNumber)) throw new Error("household assessment block changed");
    return found;
  };
  for (const facility of book.facilities) for (const amount of sizes(want)) {
    if (amount < RAW) continue;
    for (const candidate of await menu(facility, amount)) {
      const option = await quoted(candidate);
      if (!option) continue;
      const verdict = investmentQuoted(t, option.economics.usd, option.economics.new_spendable, option.economics.present_coverage,
        option.economics.premium, { reserveCost: option.economics.reserve_cost, reserveCarry: option.economics.reserve_carry,
          premiumRefundFraction: option.economics.premium_refund_fraction,
          externalPremiumSaving: option.economics.external_premium_saving });
      context.feasible++;
      bites.push(option);
      if (!verdict.go) { reject("not-worthwhile"); continue; }
      // Exact ties prefer less added coverage, then less new issuance.
      const key = [verdict.value, -option.economics.present_coverage, -option.economics.new_spendable];
      if (best === null || later(key, best.key)) best = { key, plan: option.plan, verdict, economics: option.economics };
    }
  }
  // A first bite need not repay the whole closing fee alone when the line it begins is worthwhile
  // and affordable now: score it against that same-asset programme, quoted at this block.
  const line = BigInt(Math.trunc(investmentProgramme(state, a.p) * 1e6));
  if (best === null && bites.length && fixed && line > want) {
    const programmes = [];
    for (const facility of book.facilities) for (const c of await menu(facility, line)) {
      const option = await quoted(c, true);
      if (option) programmes.push({ tokenId: c.tokenId, economics: option.economics });
    }
    context.programme_quotes = programmes.length;
    for (const { plan, economics } of bites) for (const whole of programmes) {
      if (whole.tokenId !== plan.candidate.tokenId) continue;
      const { verdict: marginal, programme: score } = stagedInvestment(t, economics, whole.economics);
      const key = [marginal.value, -economics.present_coverage, -economics.new_spendable];
      if (marginal.go && (best === null || later(key, best.key))) {
        best = { key, plan, verdict: marginal, economics };
        context.programme = { token_id: whole.tokenId, economics: whole.economics, score, bites: (line + want - 1n) / want,
          basis: "current-credit/capital/per-bite-price", fixed_cost_paid_now: plan.switch_cost,
          fixed_cost_allocated: -marginal.parts.fixed };
      }
    }
  }
  if (best === null) return done(null, NO, "no-worthwhile-investment");
  context.selected = { economics: best.economics, score: best.verdict };
  context.funding = { cash_floor: best.plan.cash_buffer + best.plan.reserve_buy_max_usdc + best.plan.switch_cost,
    basis: "selected-investment" };
  return done(best.plan, best.verdict, context.programme ? "worthwhile-staged-investment" : "worthwhile-investment");
}

/** Read-only decision of a cash investor (net_household.assess_investor). While BVIB is within its
 * tolerance of 1 ("invest") it deposits the BUCK it holds, or buys BUCK and deposits them; above it
 * ("wait") K should fall and the BUCK regain value, so it buys BUCK and holds them. One capped bite. */
export async function assessInvestor(a, world, day) {
  const m = a.refinanceMachine, cal = a.calendarMachine;
  if (!Number.isSafeInteger(day) || day < 0 || world.accounting !== "net" || m.active || m.settlementDue || m.surplusDue
      || cal.active || !cal.bound || cal.day !== day) throw new Error("investor decision needs prepared, idle canonical accounting");
  if (!a.investor) throw new Error("an insured household saves and invests through its own components");
  const s = world.session, owner = world.holderAddress(a.account), blockNumber = (await s.client.getBlock()).number;
  const at = (c, functionName, args = []) => s.client.readContract({ address: c.address, abi: c.abi, functionName, args, blockNumber });
  const cash = await at(world.usdc, "balanceOf", [owner]), held = await at(world.buck, "balanceOf", [owner]);
  const q = await observeNetMarket(s, world.buck, world.usdc, world.poolUB, world.fees.ub, { blockNumber });
  const bvib = await at(world.basket, "basketValueInBuck"), tolerance = a.p.bvib_tolerance;
  const action = netInvestorAction(bvib, tolerance);
  const context = { block: blockNumber, cash, held, bvib_wad: bvib, tolerance, action, reason: action };
  const spend = min(max(0n, cash - a.buffer), q.reserve_usdc * BigInt(Math.trunc(a.p.max_impact_bp * 50)) / 1_000_000n);
  const minimum = spend && q.net_scaled ? netPurchaseExpected(q, spend) * 9950n / 10000n : 0n;
  const buy = spend >= RAW && minimum >= RAW;
  let id = 0n;
  for (const existing of a.receipts) {
    if ((await at(world.basket, "deposits", [existing]))[2].toLowerCase() === world.buck.address.toLowerCase()) { id = existing; break; }
  }
  const base = { cash_buffer: a.buffer, spend_usdc: 0n, minimum_net: 0n, target_principal: 0n, position_id: 0n,
    redeem_shares: 0n, minimum_buck: 0n, slippage_bp: 50, deposit_buck: 0n };
  let plan = null;
  if (action === "invest" && exportable(held, 0n) >= RAW) {
    plan = { ...base, action: "convert", reserve_floor: 0n, position_id: id, deposit_buck: exportable(held, 0n) };
    context.reason = "deposit-held-buck";
  } else if (action === "invest" && buy) {
    plan = { ...base, action: "save", reserve_floor: held, spend_usdc: spend, minimum_net: minimum, position_id: id };
    context.reason = "buy-and-deposit";
  } else if (buy) {
    plan = { ...base, action: "hold", reserve_floor: held, spend_usdc: spend, minimum_net: minimum };
    context.reason = "buy-and-hold-for-lower-bvib";
  } else context.reason = action === "invest" ? "nothing-to-invest" : "hold-for-lower-bvib";
  if ((await s.client.getBlock()).number !== blockNumber) throw new Error("investor observation changed");
  return { plan, context };
}

/** Dollars a discretionary operation must leave: buffer and credit front.
 * A debtor's front comes from the whole refinance assessor; that read is
 * reused when the same day's assessment was made at this same block (a
 * declined decision sends nothing). A mortgage-free household keeps its buffer. */
export async function creditCashFloor(a, world, day, blockNumber, forecasts = {}) {
  if (!a.mortgage) return { cash_floor: a.buffer, basis: "no-external-mortgage", unfunded: false, refinance_ready: false };
  const key = `${blockNumber}:${day}:${JSON.stringify(Object.entries(forecasts).sort())}`;
  if (a.netFunding?.key !== key) {
    const funding = (await a.assessNetRefinance(world, day, forecasts)).context.funding;
    a.netFunding = { key, funding };
  }
  return a.netFunding.funding;
}

/** Read-only payday choice for the household's BUCK working balance
 * (net_household.assess_wallet). The wallet is ordinary money held apart from
 * the credit account, sized by moneyTarget and bought or trimmed in one capped
 * bite. It spends only dollars above the buffer and current credit front,
 * never the main account's BUCK, and neither issues nor retires principal. */
export async function assessWallet(a, world, day, { forecasts = {} } = {}) {
  const m = a.refinanceMachine, cal = a.calendarMachine;
  if (!Number.isSafeInteger(day) || day < 0 || world.accounting !== "net" || m.active || m.settlementDue || m.surplusDue
      || cal.active || !cal.bound || cal.day !== day) throw new Error("household wallet needs prepared, idle canonical accounting");
  if (a.netRepaymentTarget !== null || a.netTradeUncertain || a.netCancelUncertain || a.netPendingToken !== null
      || a.netRepaymentInterrupted) throw new Error("household repayment must be reconciled before working money");
  const spending = Number(a.incomeAnnual) * a.p.living_frac / 1e6;
  if (a.investor || a.sold || spending <= 0 || a.p.money_months <= 0) return { plan: null, context: { reason: "no-working-balance" } };
  const s = world.session, owner = world.holderAddress(a.account), blockNumber = (await s.client.getBlock()).number;
  const at = (c, functionName, args = []) => s.client.readContract({ address: c.address, abi: c.abi, functionName, args, blockNumber });
  const wallet = a.wallet?.address ?? "";
  const cash = await at(world.usdc, "balanceOf", [owner]), held = wallet ? await at(world.buck, "balanceOf", [wallet]) : 0n;
  const q = await observeNetMarket(s, world.buck, world.usdc, world.poolUB, world.fees.ub, { blockNumber });
  const fair = await fairUsdc(world, at, q);
  // cost/fair of a marginal net BUCK (WAD): its cost, at the dollar pool's price, carry and fee,
  // over its fair dollar value.
  const ratio = q.net_scaled && fair.num ? q.price_num * q.gross_scaled * 1_000_000n * 10n ** 18n * fair.den
    / (q.price_den * q.net_scaled * (1_000_000n - q.fee_ppm) * fair.num) : null;
  const target = netWalletTarget(ratio, day - a.arrive_day, spending, a.p);
  const credit = await creditCashFloor(a, world, day, blockNumber, forecasts);
  const floor = max(a.buffer, credit.cash_floor), choice = netWalletAdjustment(q, cash, held, target, floor, a.p);
  const context = { block: blockNumber, cash, wallet, held, target, credit_funding: credit, choice,
    marginal_ratio_wad: ratio, fair_basis: fair.basis, reason: choice.reason };
  const plan = ["buy", "sell"].includes(choice.action) ? { action: choice.action, wallet, cash_buffer: floor, target, held,
    spend_usdc: choice.spend_usdc, minimum_net: choice.minimum_net, sell_buck: choice.sell_buck,
    minimum_usdc: choice.minimum_usdc, slippage_bp: 50 } : null;
  if ((await s.client.getBlock()).number !== blockNumber) throw new Error("household wallet observation changed");
  return { plan, context };
}

export async function assessSurplus(a, world, day, { forecasts = {} } = {}) {
  const m = a.refinanceMachine, cal = a.calendarMachine;
  if (!Number.isSafeInteger(day) || day < 0 || world.accounting !== "net" || m.active || m.settlementDue
      || cal.active || !cal.bound || cal.day !== day) throw new Error("household surplus needs prepared, idle canonical accounting");
  if (a.savings || a.netRepaymentTarget !== null || a.netTradeUncertain || a.netCancelUncertain || a.netPendingToken !== null
      || a.netRepaymentInterrupted || m.surplusDue) throw new Error("household custody/repayment must be reconciled before surplus");
  const s = world.session, owner = world.holderAddress(a.account), ids = world._credits.get(a.account.address) ?? [];
  const blockNumber = (await s.client.getBlock()).number;
  const at = (c, functionName, args = []) => s.client.readContract({ address: c.address, abi: c.abi, functionName, args, blockNumber });
  for (const id of ids) if ((await at(world.buck, "reductionAuthorization", [id]))[0]) throw new Error("household repayment authorization remains pending");
  const cash = await at(world.usdc, "balanceOf", [owner]), held = await at(world.buck, "balanceOf", [owner]);
  const book = await world.netCreditOps.household(owner, ids, { blockNumber });
  const q = await observeNetMarket(s, world.buck, world.usdc, world.poolUB, world.fees.ub, { blockNumber });
  const fair = await fairUsdc(world, at, q), fairNum = fair.num, fairDen = fair.den;
  const credit = await creditCashFloor(a, world, day, blockNumber, forecasts);
  const floor = max(a.buffer, credit.cash_floor), choice = netSurplusPurchase(q, cash, book.issued, fairNum, fairDen, floor, a.p);
  // Fair value is the basket's USDC value by its best routes (fairUsdc); the
  // purchase cost carries the dollar pool's price, carry and fee.
  const context = { block: blockNumber, cash, protected_buck: held, credit_funding: credit, choice,
    fair_num: fairNum, fair_den: fairDen, fair_basis: fair.basis, reason: choice.reason };
  let plan = null;
  if (choice.action === "retire" && a.receipts.length) {
    const positions = await markNetBasketPositions(s, world.basket, world.buck, owner, a.receipts, { blockNumber });
    context.basket_exits = [];
    for (const p of positions.positions) {
      if (!p.mark || !p.shares) continue;
      const amount = min(p.shares, (choice.target_principal * p.shares + p.mark - 1n) / p.mark);
      if (!amount) continue;
      try {
        const r = await basketOps(world).previewRedemption(owner, p.tokenId, amount), minimum = r.paid * 9950n / 10000n;
        if (r.blockNumber !== blockNumber) throw new Error("basket exit observation changed");
        if (minimum >= RAW) {
          plan = { action: "retire", cash_buffer: floor, reserve_floor: held, target_principal: choice.target_principal,
            position_id: p.tokenId, redeem_shares: amount, minimum_buck: minimum, spend_usdc: 0n, minimum_net: 0n, slippage_bp: 50 };
          context.reason = "redeem-to-retire";
          context.exit = { block_number: r.blockNumber, position_id: r.positionId, paid: r.paid,
            shares_retired: r.sharesRetired, shares_remaining: r.sharesRemaining }; break;
        }
      } catch (err) { context.basket_exits.push({ token_id: p.tokenId, unavailable: String(err).slice(0, 200) }); }
    }
  }
  if (!plan && choice.spend_usdc) {
    let id = 0n;
    if (choice.action === "save") for (const existing of a.receipts) {
      if ((await at(world.basket, "deposits", [existing]))[2].toLowerCase() === world.buck.address.toLowerCase()) { id = existing; break; }
    }
    plan = { action: choice.action, cash_buffer: floor, reserve_floor: held, spend_usdc: choice.spend_usdc,
      minimum_net: choice.minimum_net, target_principal: choice.target_principal, position_id: id,
      redeem_shares: 0n, minimum_buck: 0n, slippage_bp: 50 };
    context.reason = `purchase-to-${choice.action}`;
  }
  if ((await s.client.getBlock()).number !== blockNumber) throw new Error("household surplus observation changed");
  return { plan, context };
}

export class HouseholdRefinance {
  constructor(agent) { this.agent = agent; this.flow = null; this.events = []; this.decisions = 0; }
  get active() { return this.flow !== null && ["running", "pending"].includes(this.flow.status); }
  get settlementDue() { return this.flow !== null && this.flow.status === "refused" && ["export", "mortgage"].includes(this.flow.phase); }
  get paymentDue() { return this.settlementDue && this.flow.phase === "mortgage"; }
  /** A known refused basket deposit of owned BUCK: saved cash, or a confirmed credit issue. */
  get surplusDue() {
    return Boolean(["surplus", "invest"].includes(this.flow?.kind) && this.flow.status === "refused" && this.flow.phase === "deposit");
  }
  label() { return ["surplus", "wallet", "invest", "exit"].includes(this.flow?.kind) ? this.flow.kind : "refinance"; }
  /** Close a KNOWN refused remainder whose intent no longer applies (the household is leaving).
   * Confirmed steps stay real holdings; nothing is reversed or sent. An unknown outcome cannot be. */
  async abandon(world, day, reason) {
    if (!(this.settlementDue || this.surplusDue)) throw new Error("only a known refused remainder can be abandoned");
    this.flow.status = "abandoned"; this.agent.fundingPending = false;
    await this.event(world, day, "abandoned", { reason: reason.slice(0, 200) });
  }
  /** The selected-facility engine's retained intent; null when idle. A funded
   * target is an intention with protected prior cash, not a burn. `uncertain`
   * must be reconciled before any further send; `waiting` is a known refusal
   * to be re-read later. */
  retirement() {
    const a = this.agent;
    const pending = a.netPendingToken === null ? null : { token_id: a.netPendingToken, principal: a.netPendingPrincipal,
      phase: a.netPendingPhase, hash: a.netPendingHash, reverted: a.netPendingReverted,
      authorization_confirmed: a.netAuthorizationConfirmed };
    const uncertain = Boolean(a.netTradeUncertain || a.netCancelUncertain || pending);
    if (a.netRepaymentTarget === null && a.netRepaymentReserve === null && !uncertain) return null;
    return { target: a.netRepaymentTarget, reserve: a.netRepaymentReserve, uncertain, waiting: Boolean(a.netRepaymentInterrupted),
      pending, cancel_hash: a.netCancelUncertain ? a.netCancelHash : null };
  }
  status() {
    const f = this.flow;
    return { schema: 1, decision: this.decisions, status: f?.status ?? "idle", phase: f?.phase ?? null,
      hash: f?.hash ?? null, payment_due: this.paymentDue, settlement_due: this.settlementDue,
      kind: f?.kind ?? (f ? "refinance" : null), surplus_due: this.surplusDue,
      last_surplus_day: this.agent.netSurplusDay, next_surplus_day: this.agent.nextSurplus,
      next_wallet_day: this.agent.nextWallet,
      last_attempt_day: this.agent.netRefinanceDay, next_decision_day: this.agent.nextDecision,
      credit_reserve: this.agent.netCreditReserve, retirement: this.retirement(),
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
      // One surplus operation's own floors are never the lasting policy.
      const surplus = this.flow?.kind === "surplus";
      const buffer = ["surplus", "wallet", "exit"].includes(this.flow?.kind) ? a.buffer : this.flow?.plan.cash_buffer ?? a.buffer;
      const reserve = a.netCreditReserve, custody = {};
      const protection = surplus ? { usdc: this.flow.plan.cash_buffer, buck: this.flow.plan.reserve_floor } : null;
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
        cash_buffer: buffer, reserve_floor: reserve, surplus_protection: protection,
        ratios: { utilization: limit ? Number(issued) / Number(limit) : null,
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
    if (["surplus", "wallet", "invest", "exit"].includes(this.flow?.kind)) detail = { kind: this.flow.kind, ...detail };
    this.events.push({ schema: 1, seq: this.events.length + 1, member: this.agent.member, decision: this.decisions,
      day, block, phase: this.flow?.phase ?? "idle", status, accounts: await this.accounts(world), ...wire(detail) });
  }
  // One quoted working-balance trade on the same receipt cursor. Dollars buy
  // net BUCK delivered to the wallet, or the wallet sells BUCK for dollars
  // delivered home; credit and the main account's BUCK are untouched.
  async startWallet(world, p, day, ctr = {}, { context = {} } = {}) {
    if (this.active || this.settlementDue || this.surplusDue) throw new Error("household has unfinished cash work");
    if (!p || !["buy", "sell"].includes(p.action)) throw new Error("invalid household wallet plan");
    const a = this.agent, buying = p.action === "buy";
    const values = [p.cash_buffer, p.target, p.held, p.spend_usdc, p.minimum_net, p.sell_buck, p.minimum_usdc];
    if (values.some((v) => typeof v !== "bigint" || v < 0n || v > MAX) || !Number.isSafeInteger(p.slippage_bp)
        || p.slippage_bp < 0 || p.slippage_bp > 1000 || p.cash_buffer < a.buffer || !a.wallet
        || typeof p.wallet !== "string" || p.wallet.toLowerCase() !== a.wallet.address.toLowerCase()
        || buying !== Boolean(p.spend_usdc && p.minimum_net) || buying === Boolean(p.sell_buck && p.minimum_usdc)
        || buying && (p.sell_buck || p.minimum_usdc) || !buying && (p.spend_usdc || p.minimum_net)) {
      throw new Error("invalid household wallet bounds");
    }
    this.decisions++; this.counters = ctr;
    this.flow = { kind: "wallet", plan: { ...p }, phase: p.action, status: "running", hash: null, terms: null, revision: 0 };
    await this.event(world, day, "planned", { plan: p, context, market: await this.market(world) });
    return this.advance(world, day, ctr);
  }
  async prepareWallet(world, day) {
    const a = this.agent, p = this.flow.plan, s = world.session, owner = world.holderAddress(a.account);
    if (p.action === "buy") {
      if (await s.call(world.usdc, "balanceOf", [owner]) < p.cash_buffer + p.spend_usdc) {
        await this.refuse(world, day, "wallet USDC funding reserve changed"); return false;
      }
    } else if (await s.call(world.buck, "balanceOf", [p.wallet]) < p.sell_buck) {
      await this.refuse(world, day, "wallet BUCK balance changed"); return false;
    }
    return true;
  }
  async applyWallet(world, day, r) {
    const a = this.agent, f = this.flow, p = f.plan;
    const receipt = r.receipt ?? { transactionHash: r.transactionHash, blockNumber: r.blockNumber };
    let amounts;
    if (p.action === "buy") {
      if (r.paid <= 0n || r.paid > p.spend_usdc || r.received < p.minimum_net) throw new Error("wallet purchase differs from bounds");
      a.walletBought += r.received;
      amounts = { usdc: r.paid, net_buck_scaled: r.receivedScaled, nominal_buck: r.nominalReceived };
    } else {
      if (r.paid <= 0n || r.paid > p.sell_buck || r.received < p.minimum_usdc) throw new Error("wallet sale differs from bounds");
      a.walletSold += r.paid;
      amounts = { buck: r.paid, nominal_buck: r.nominalPaid, usdc: r.received };
    }
    // Dollars against BUCK at one-for-one, the households' existing trade-cost convention.
    a.tradeLoss += r.paid - r.received;
    f.hash = null; f.terms = null; f.status = "running"; f.phase = "complete";
    await this.event(world, day, "confirmed", { operation: p.action, amounts,
      transaction_hash: receipt.transactionHash, receipt_block: receipt.blockNumber ?? null });
  }
  // One cash-only intention, with the same receipt cursor as refinancing.
  // New money never becomes a burn until the existing facility engine settles.
  async startSurplus(world, given, day, ctr = {}, { context = {} } = {}) {
    let p = given;
    if (this.active || this.settlementDue || this.surplusDue) throw new Error("household has unfinished cash work");
    if (!p || !["save", "retire", "hold", "convert"].includes(p.action)) throw new Error("invalid household surplus plan");
    p = { deposit_buck: 0n, ...p };
    const values = [p.cash_buffer, p.reserve_floor, p.spend_usdc, p.minimum_net, p.target_principal,
      p.position_id, p.redeem_shares, p.minimum_buck, p.deposit_buck];
    const convert = p.action === "convert";          // owned BUCK deposited; nothing bought or redeemed
    if (values.some((v) => typeof v !== "bigint" || v < 0n || v > MAX) || !Number.isSafeInteger(p.slippage_bp)
        || p.slippage_bp < 0 || p.slippage_bp > 1000 || p.cash_buffer < this.agent.buffer
        || Boolean(p.spend_usdc) !== Boolean(p.minimum_net) || Boolean(p.redeem_shares) !== Boolean(p.minimum_buck)
        || convert !== Boolean(p.deposit_buck) || convert && (p.spend_usdc || p.redeem_shares)
        || !convert && Boolean(p.redeem_shares) === Boolean(p.spend_usdc)
        || p.action !== "retire" && (p.redeem_shares || p.target_principal) || p.action === "retire" && !p.target_principal
        || p.redeem_shares && !p.position_id) throw new Error("invalid household surplus bounds");
    const a = this.agent, owner = world.holderAddress(a.account), s = world.session;
    const cash = await s.call(world.usdc, "balanceOf", [owner]);
    if (p.spend_usdc && cash < p.cash_buffer + p.spend_usdc) throw new Error("surplus would spend protected credit capital");
    if (await s.call(world.buck, "balanceOf", [owner]) < p.reserve_floor) throw new Error("household surplus reserve changed");
    const book = await world.netCreditOps.household(owner, world._credits.get(a.account.address) ?? []);
    if (p.target_principal > book.issued) throw new Error("surplus retirement exceeds actual principal");
    if (p.position_id && !a.receipts.includes(p.position_id)) throw new Error("household surplus names an untracked receipt");
    this.decisions++; this.counters = ctr;
    this.flow = { kind: "surplus", plan: { ...p }, phase: convert ? "deposit" : p.redeem_shares ? "redeem" : "buy",
      status: "running", hash: null, terms: null, revision: 0, acquired: p.deposit_buck };
    await this.event(world, day, "planned", { plan: p, context, market: await this.market(world), basket: await this.basket(world) });
    return this.advance(world, day, ctr);
  }
  async resumeSurplus(world, day, ctr = this.counters ?? {}) {
    if (!this.surplusDue) throw new Error("household has no known refused savings deposit");
    Object.assign(this.flow, { status: "running", hash: null, terms: null, revision: this.flow.revision + 1 });
    await this.event(world, day, "resumed", { reason: "retain purchased cash; re-quote only the deposit" });
    return this.advance(world, day, ctr);
  }
  async prepareSurplus(world, day) {
    const a = this.agent, f = this.flow, p = f.plan, s = world.session, owner = world.holderAddress(a.account);
    const held = await s.call(world.buck, "balanceOf", [owner]);
    if (f.phase === "buy") {
      if (await s.call(world.usdc, "balanceOf", [owner]) < p.cash_buffer + p.spend_usdc) {
        await this.refuse(world, day, "surplus USDC funding reserve changed"); return false;
      }
      f.terms = { preserved: max(held, p.reserve_floor) + 1n };
    } else if (f.phase === "deposit") {
      return this.prepareDeposit(world, day, f.acquired);
    } else {
      f.terms = await basketOps(world).prepareRedemption(owner, p.position_id, p.redeem_shares, p.minimum_buck);
      f.preserved = max(held, p.reserve_floor) + 1n;
      await this.event(world, day, "quoted", { basket_terms: Object.fromEntries(Object.entries(f.terms).map(([k, v]) => [snake(k), v])) });
    }
    return true;
  }
  async applySurplus(world, day, r, ctr) {
    const a = this.agent, f = this.flow, p = f.plan, phase = f.phase;
    const receipt = r.receipt ?? { transactionHash: r.transactionHash, blockNumber: r.blockNumber };
    let amounts, preserved;
    if (phase === "buy") {
      if (r.paid <= 0n || r.paid > p.spend_usdc || r.received < p.minimum_net) throw new Error("surplus purchase differs from bounds");
      a.tradeLoss += r.paid - r.received;
      if (["save", "hold"].includes(p.action)) a.investedUsd += r.paid;
      f.acquired = r.received;
      amounts = { usdc: r.paid, net_buck_scaled: r.receivedScaled, nominal_buck: r.nominalReceived };
      preserved = f.terms.preserved; f.phase = p.action === "save" ? "deposit" : "complete";
    } else {
      const id = r.tokenId, old = a.book.get(id) ?? { principal: 0n, shares: 0n };
      if (old.principal !== f.terms.priorBasis || old.shares !== f.terms.priorShares) {
        throw new Error("household basket books differ from prepared receipt");
      }
      if (phase === "deposit") amounts = this.bookDeposit(world, r, ctr);
      else {
        const shares = r.sharesRemaining, basis = r.basisRemaining;
        preserved = f.preserved;
        amounts = { buck: r.paid, token_id: id, shares_retired: r.sharesRetired, in_kind: r.inKind };
        a.basketPrincipal += basis - old.principal; a.shares += shares - old.shares;
        if (shares) a.book.set(id, { principal: basis, shares });
        else { a.book.delete(id); a.receipts = a.receipts.filter((v) => v !== id); }
      }
      f.phase = "complete";
    }
    if (p.action === "retire") { a.netRepaymentReserve = preserved; a.netRepaymentTarget = p.target_principal; }
    f.hash = null; f.terms = null; f.status = "running";
    await this.event(world, day, "confirmed", { operation: phase, amounts,
      transaction_hash: receipt.transactionHash, receipt_block: receipt.blockNumber ?? null });
  }

  /** Quote a basket deposit of owned BUCK above the reserve; false when none is sent. */
  async prepareDeposit(world, day, wanted) {
    const a = this.agent, f = this.flow, p = f.plan, s = world.session, owner = world.holderAddress(a.account);
    const amount = min(wanted, exportable(await s.call(world.buck, "balanceOf", [owner]), p.reserve_floor));
    // Priced as the basket prices a deposit (high marks, less its charge), not its TWAP share price.
    const expected = amount ? await basketOps(world).quoteDepositShares(amount) : 0n;
    const minimum = expected * BigInt(10000 - p.slippage_bp) / 10000n;
    if (!minimum) {
      f.phase = "complete"; f.retained = true;
      await this.event(world, day, "retained", { reason: "owned cash below deposit grain/reserve" }); return false;
    }
    f.terms = await basketOps(world).prepareDeposit(owner, amount, minimum, { tokenId: p.position_id });
    await this.event(world, day, "quoted", { basket_terms: Object.fromEntries(Object.entries(f.terms).map(([k, v]) => [snake(k), v])) });
    return true;
  }
  /** Apply one authenticated deposit to the household's receipt books, once. */
  bookDeposit(world, r, ctr) {
    const a = this.agent, f = this.flow, id = r.tokenId, old = a.book.get(id) ?? { principal: 0n, shares: 0n };
    if (old.principal !== f.terms.priorBasis || old.shares !== f.terms.priorShares) {
      throw new Error("household basket books differ from prepared receipt");
    }
    if (!a.receipts.includes(id)) { a.receipts.push(id); world.receipts++; }
    a.basketPrincipal += r.basis - old.principal; a.shares += r.shares - old.shares;
    a.book.set(id, { principal: r.basis, shares: r.shares });
    ctr.bcdInvested = (ctr.bcdInvested ?? 0n) + r.received; ctr.bcdInvestments = (ctr.bcdInvestments ?? 0) + 1;
    return { buck: r.received, token_id: id, shares_added: r.sharesAdded };
  }
  /** Re-read the saved candidate; true when its unchanged terms may be submitted. */
  async prepareIssue(world, day, label) {
    const a = this.agent, f = this.flow, p = f.plan, c = p.candidate, owner = world.holderAddress(a.account);
    let fresh;
    try { fresh = await world.netCreditOps.assessIssueCandidate(owner, c.tokenId, c.coverage,
      c.requestedSpendable, c.maxPremium, { reserveBuffer: c.reserveBuffer }); }
    catch (err) {
      a.lastError = `${label} issuance observation unavailable: ${err}`.slice(0, 200);
      await this.event(world, day, "waiting", { reason: a.lastError }); return false;
    }
    if (fresh.reasons.length || ["priorPrincipal", "beforeCoverage", "afterCoverage", "premium"].some((key) => fresh[key] !== c[key])
        || fresh.premiumPool.toLowerCase() !== c.premiumPool.toLowerCase() || reserveFor(fresh) > p.reserve_floor) {
      await this.refuse(world, day, `${label} issuance terms changed; reassess retained holdings`); return false;
    }
    f.issue_candidate = fresh;
    return true;
  }

  /** One step of a leaving household's unwinding (net_household.start_exit): a receipt redeemed
   * home, owned BUCK sold for dollars, or burn(0) releasing coverage no principal needs. Principal is
   * retired only by the selected-facility engine; none of these issues or retires any. */
  async startExit(world, given, day, ctr = {}, { context = {} } = {}) {
    if (this.active || this.settlementDue || this.surplusDue) throw new Error("household has unfinished cash work");
    const a = this.agent, ids = (world._credits.get(a.account.address) ?? []).map(String);
    if (!given || !EXIT_STEPS.includes(given.step)) throw new Error("invalid household exit plan");
    const p = { position_id: 0n, shares: 0n, minimum_buck: 0n, sell_buck: 0n, minimum_usdc: 0n, token_ids: [], ...given };
    const [redeem, sell, release] = EXIT_STEPS.map((step) => p.step === step);
    if ([p.position_id, p.shares, p.minimum_buck, p.sell_buck, p.minimum_usdc].some((v) => typeof v !== "bigint" || v < 0n || v > MAX)
        || !Array.isArray(p.token_ids) || redeem !== Boolean(p.position_id && p.shares && p.minimum_buck)
        || sell !== Boolean(p.sell_buck && p.minimum_usdc) || release !== Boolean(p.token_ids.length)
        || redeem && !a.receipts.includes(p.position_id)
        || release && (new Set(p.token_ids.map(String)).size !== p.token_ids.length || p.token_ids.some((t) => !ids.includes(String(t))))) {
      throw new Error("invalid household exit bounds");
    }
    this.decisions++; this.counters = ctr;
    this.flow = { kind: "exit", plan: p, phase: p.step, status: "running", hash: null, terms: null, revision: 0 };
    await this.event(world, day, "planned", { plan: p, context: wire(context), market: await this.market(world), basket: await this.basket(world) });
    return this.advance(world, day, ctr);
  }
  async prepareExit(world, day) {
    const a = this.agent, f = this.flow, p = f.plan, owner = world.holderAddress(a.account);
    if (p.step === "redeem") {
      f.terms = await basketOps(world).prepareRedemption(owner, p.position_id, p.shares, p.minimum_buck);
      await this.event(world, day, "quoted", { basket_terms: Object.fromEntries(Object.entries(f.terms).map(([k, v]) => [snake(k), v])) });
    } else if (p.step === "sell" && await world.session.call(world.buck, "balanceOf", [owner]) < p.sell_buck) {
      await this.refuse(world, day, "exit BUCK balance changed"); return false;
    }
    return true;
  }
  async applyExit(world, day, r, ctr) {
    const a = this.agent, f = this.flow, p = f.plan;
    const receipt = r.receipt ?? { transactionHash: r.transactionHash, blockNumber: r.blockNumber };
    let amounts;
    if (p.step === "redeem") {
      const id = r.tokenId, old = a.book.get(id) ?? { principal: 0n, shares: 0n };
      if (old.principal !== f.terms.priorBasis || old.shares !== f.terms.priorShares) {
        throw new Error("household basket books differ from prepared receipt");
      }
      a.basketPrincipal += r.basisRemaining - old.principal; a.shares += r.sharesRemaining - old.shares;
      if (r.sharesRemaining) a.book.set(id, { principal: r.basisRemaining, shares: r.sharesRemaining });
      else { a.book.delete(id); a.receipts = a.receipts.filter((v) => v !== id); }
      ctr.hhRedeemed = (ctr.hhRedeemed ?? 0) + 1;
      amounts = { buck: r.paid, token_id: id, shares_retired: r.sharesRetired, in_kind: r.inKind };
    } else if (p.step === "sell") {
      if (r.paid <= 0n || r.paid > p.sell_buck || r.received < p.minimum_usdc) throw new Error("exit sale differs from bounds");
      a.tradeLoss += r.paid - r.received; a.withdrawnUsd += r.received;
      amounts = { buck: r.paid, nominal_buck: r.nominalPaid, usdc: r.received };
    } else {
      a.releasedCoverage += r.coverage; a.releasedRefund += r.refund;
      amounts = { coverage: r.coverage, premium_refund_nominal: r.refund,
        facilities: r.released.map((x) => ({ token_id: x.tokenId, coverage: x.coverage, premium_refund_nominal: x.refund })) };
    }
    f.hash = null; f.terms = null; f.status = "running"; f.phase = "complete";
    await this.event(world, day, "confirmed", { operation: p.step, amounts,
      transaction_hash: receipt.transactionHash, receipt_block: receipt.blockNumber ?? null });
  }

  /** One quoted credit investment on the existing cursor (net_household.start_investment): the
   * closing fee to the bank first, a reserve purchase, the issue, and a basket deposit of the new
   * BUCK under this owner. A refused deposit keeps the BUCK owned and is retried alone (surplusDue). */
  async startInvestment(world, given, day, ctr = {}, { context = {} } = {}) {
    if (this.active || this.settlementDue || this.surplusDue) throw new Error("household has unfinished cash work");
    const a = this.agent, c = given?.candidate, owner = world.holderAddress(a.account);
    const ids = world._credits.get(a.account.address) ?? [];
    if (!given || !c || !Array.isArray(c.reasons)) throw new Error("invalid household investment plan");
    const p = { switch_cost: 0n, reserve_buy_net: 0n, reserve_buy_max_usdc: 0n, position_id: 0n, slippage_bp: 50, ...given };
    const values = [p.deposit_buck, p.cash_buffer, p.reserve_floor, p.switch_cost, p.reserve_buy_net, p.reserve_buy_max_usdc, p.position_id];
    const funding = p.reserve_buy_net > 0n;
    if (values.some((v) => typeof v !== "bigint" || v < 0n || v > MAX) || !Number.isSafeInteger(p.slippage_bp)
        || p.slippage_bp < 0 || p.slippage_bp > 1000 || p.deposit_buck <= 0n || p.deposit_buck > c.requestedSpendable
        || p.cash_buffer < a.buffer || Boolean(p.reserve_buy_max_usdc) !== funding || a.mortgage || a.investor) {
      throw new Error("invalid household investment bounds");
    }
    if (c.reasons.some((reason) => reason !== "reserve-shortfall" || !funding) || !ids.some((id) => id === c.tokenId)
        || p.reserve_buy_net < c.reserveShortfall || p.reserve_floor < reserveFor(c)) {
      throw new Error("unavailable household investment candidate");
    }
    if (a.refinanceClosed && p.switch_cost) throw new Error("household closing was already paid");
    if (p.position_id && !a.receipts.includes(p.position_id)) throw new Error("household investment names an untracked receipt");
    for (const id of ids) if ((await world.session.call(world.buck, "reductionAuthorization", [id]))[0]) {
      throw new Error("household reduction authorization pending");
    }
    if (await world.session.call(world.usdc, "balanceOf", [owner]) < p.cash_buffer + p.reserve_buy_max_usdc + p.switch_cost) {
      throw new Error("investment would consume the USDC buffer");
    }
    this.decisions++; this.counters = ctr;
    a.netCreditReserve = p.reserve_floor;
    this.flow = { kind: "invest", plan: { ...p, candidate: { ...c, reasons: [...c.reasons] } }, status: "running", hash: null,
      terms: null, revision: 0, acquired: 0n, issue_candidate: null,
      phase: p.switch_cost ? "closing" : funding ? "fund" : "issue" };
    a.fundingPending = true;
    const limits = Object.fromEntries(["surplus_policy", "theta", "issue_premium", "carry_utilization", "utilization", "max_impact_bp"]
      .map((k) => [k, a.p[k]]));
    const plan = { ...this.flow.plan, candidate: Object.fromEntries(Object.entries(c).map(([k, v]) => [snake(k), v])) };
    await this.event(world, day, "planned", { plan, context: wire(context), limits, market: await this.market(world),
      basket: await this.basket(world) });
    return this.advance(world, day, ctr);
  }
  async prepareInvest(world, day) {
    const a = this.agent, f = this.flow, p = f.plan, owner = world.holderAddress(a.account), s = world.session;
    if (f.phase === "closing") {
      if (await s.call(world.usdc, "balanceOf", [owner]) < p.switch_cost + p.reserve_buy_max_usdc + p.cash_buffer) {
        await this.refuse(world, day, "investment USDC buffer changed"); return false;
      }
      f.terms = { amount: p.switch_cost };
    } else if (f.phase === "fund") {
      if (await s.call(world.usdc, "balanceOf", [owner]) < p.reserve_buy_max_usdc + p.cash_buffer) {
        await this.refuse(world, day, "investment reserve USDC buffer changed"); return false;
      }
    } else if (f.phase === "issue") return this.prepareIssue(world, day, "investment");
    else return this.prepareDeposit(world, day, min(f.acquired, p.deposit_buck));
    return true;
  }
  async applyInvest(world, day, r, ctr) {
    const a = this.agent, f = this.flow, p = f.plan, phase = f.phase;
    const receipt = r.receipt ?? { transactionHash: r.transactionHash, blockNumber: r.blockNumber };
    let amounts;
    if (phase === "closing") {
      if (r.amount !== f.terms.amount) throw new Error("household closing payment changed");
      a.switchPaid += r.amount; a.refinanceClosed = true;
      amounts = { usdc: r.amount, switch_usdc: r.amount };
      f.phase = p.reserve_buy_net ? "fund" : "issue";
    } else if (phase === "fund") {
      if (r.paid <= 0n || r.paid > p.reserve_buy_max_usdc || r.received < p.reserve_buy_net) {
        throw new Error("household reserve purchase does not match bounds");
      }
      a.tradeLoss += r.paid - r.received;
      amounts = { usdc: r.paid, net_buck_scaled: r.receivedScaled, nominal_buck: r.nominalReceived };
      f.phase = "issue";
    } else if (phase === "issue") {
      a.premiumPaid += r.premium; a.joined = true; f.acquired = r.spendable;
      amounts = { principal: r.principal, premium: r.premium, spendable: r.spendable };
      f.phase = "deposit";
    } else {
      amounts = this.bookDeposit(world, r, ctr);
      a.issuedInvest += r.received; a.deploys++;
      f.phase = "complete";
    }
    f.hash = null; f.terms = null; f.status = "running";
    await this.event(world, day, "confirmed", { operation: phase, amounts,
      transaction_hash: receipt.transactionHash, receipt_block: receipt.blockNumber ?? null });
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
    // Policy state outlives this cursor; later saving/retirement neither
    // replaces it with its own protection nor releases it.
    a.netCreditReserve = p.reserve_floor;
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
    this.agent.lastAction = `${this.label()}-refused`;
    await this.event(world, day, "refused", { reason: reason.slice(0, 200), transaction_hash: this.flow.hash });
  }
  async apply(world, day, result, ctr) {
    if (this.flow.kind === "surplus") return this.applySurplus(world, day, result, ctr);
    if (this.flow.kind === "wallet") return this.applyWallet(world, day, result);
    if (this.flow.kind === "invest") return this.applyInvest(world, day, result, ctr);
    if (this.flow.kind === "exit") return this.applyExit(world, day, result, ctr);
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
      f.status = "pending"; this.agent.lastAction = `${this.label()}-pending`;
      this.agent.lastError = `${this.label()} accounting unavailable: ${err}`.slice(0, 200);
      await this.event(world, day, "pending", { reason: this.agent.lastError, transaction_hash: f.hash }); return false;
    }
  }
  async recover(world) {
    const f = this.flow, a = this.agent, owner = world.holderAddress(a.account), p = f.plan;
    const receipt = await world.session.client.getTransactionReceipt({ hash: f.hash });
    if (receipt?.transactionHash?.toLowerCase() !== f.hash.toLowerCase()) throw new Error("household receipt hash mismatch");
    if (receipt.status === "reverted") throw new TransactionReverted(f.phase, "household operation reverted", receipt);
    if (f.kind === "wallet") {
      if (f.phase === "buy") return { ...netPurchaseFromReceipt(world, world.netMarket, { address: owner }, receipt, f.hash, p.wallet), receipt };
      return netSaleFromReceipt(world, world.netMarket, { address: p.wallet }, receipt,
        { expectedHash: f.hash, expectedInput: p.sell_buck, minimumOut: p.minimum_usdc, recipient: owner });
    }
    if (f.kind === "surplus") {
      if (f.phase === "buy") return { ...netPurchaseFromReceipt(world, world.netMarket, { address: owner }, receipt, f.hash), receipt };
      const ops = basketOps(world);
      return f.phase === "deposit" ? ops.depositFromReceipt(f.terms, receipt, f.hash) : ops.redemptionFromReceipt(f.terms, receipt, f.hash);
    }
    if (f.kind === "exit") {
      if (p.step === "redeem") return basketOps(world).redemptionFromReceipt(f.terms, receipt, f.hash);
      if (p.step === "sell") return netSaleFromReceipt(world, world.netMarket, { address: owner }, receipt,
        { expectedHash: f.hash, expectedInput: p.sell_buck, minimumOut: p.minimum_usdc });
      return world.netCreditOps.releaseFromReceipt(owner, p.token_ids, receipt, f.hash);
    }
    if (f.kind === "invest" && f.phase === "closing") return world.netSettlementOps.paymentFromReceipt(owner, BANK, f.terms.amount, receipt, f.hash);
    if (f.kind === "invest" && f.phase === "deposit") return basketOps(world).depositFromReceipt(f.terms, receipt, f.hash);
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
      if (!f.hash) { a.lastError = `${this.label()} hash unavailable; reconcile before continuing`; return true; }
      let result;
      try { result = await this.recover(world); }
      catch (err) {
        if (err instanceof TransactionReverted) await this.refuse(world, day, String(err));
        else a.lastError = `${this.label()} receipt unavailable: ${err}`.slice(0, 200);
        return true;
      }
      if (!await this.applyKnown(world, day, result, ctr)) return true;
    }
    while (f.phase !== "complete") {
      const p = f.plan, phase = f.phase;
      if (f.kind === "surplus") {
        let prepared;
        try { prepared = await this.prepareSurplus(world, day); }
        catch (err) { await this.refuse(world, day, `surplus observation unavailable: ${err}`); return true; }
        if (!prepared) { if (f.phase === "complete") continue; return true; }
      } else if (f.kind === "wallet") {
        let prepared;
        try { prepared = await this.prepareWallet(world, day); }
        catch (err) { await this.refuse(world, day, `wallet observation unavailable: ${err}`); return true; }
        if (!prepared) return true;
      } else if (f.kind === "exit") {
        let prepared;
        try { prepared = await this.prepareExit(world, day); }
        catch (err) { await this.refuse(world, day, `exit observation unavailable: ${err}`); return true; }
        if (!prepared) return true;
      } else if (f.kind === "invest") {
        let prepared;
        try { prepared = await this.prepareInvest(world, day); }
        catch (err) { await this.refuse(world, day, `investment observation unavailable: ${err}`); return true; }
        if (!prepared) { if (f.phase === "complete") continue; return true; }
      } else if (phase === "issue") {
        // A confirmed reserve buy stays committed while an unavailable view
        // is retried. Neither that retry nor a changed quote buys twice.
        if (!await this.prepareIssue(world, day, "refinance")) return true;
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
        if (f.kind === "wallet") {
          if (phase === "buy") result = await buyBuck(world, world.netMarket, { address: owner }, p.spend_usdc,
            { minOut: p.minimum_net, recipient: p.wallet });
          else result = await sellBuck(world, world.netMarket, { address: p.wallet }, p.sell_buck,
            { minOut: p.minimum_usdc, recipient: owner });
        } else if (f.kind === "surplus") {
          if (phase === "buy") result = await buyBuck(world, world.netMarket, { address: owner }, p.spend_usdc, { minOut: p.minimum_net });
          else if (phase === "deposit") result = await basketOps(world).deposit(f.terms);
          else result = await basketOps(world).redeem(f.terms);
        } else if (f.kind === "exit") {
          if (p.step === "redeem") result = await basketOps(world).redeem(f.terms);
          else if (p.step === "sell") result = await sellBuck(world, world.netMarket, { address: owner }, p.sell_buck,
            { minOut: p.minimum_usdc });
          else result = await world.netCreditOps.release(owner, p.token_ids);
        } else if (f.kind === "invest" && phase === "closing") result = await world.netSettlementOps.payExact(owner, BANK, f.terms.amount);
        else if (f.kind === "invest" && phase === "deposit") result = await basketOps(world).deposit(f.terms);
        else if (phase === "fund") result = await buyBuckExact(world, world.netMarket, { address: owner },
          p.reserve_buy_net, p.reserve_buy_max_usdc);
        else if (phase === "issue") result = await world.netCreditOps.issueCandidate(owner, f.issue_candidate);
        else if (phase === "export") result = await sellBuck(world, world.netMarket, { address: owner }, p.export_buck,
          { minOut: p.minimum_usdc });
        else result = await world.netSettlementOps.payExact(owner, BANK, f.terms.amount);
      } catch (err) {
        f.hash = err.transactionHash ?? null;
        const reverted = (err instanceof IssuancePending || err instanceof ReleasePending) && err.reverted
          || err instanceof TransactionReverted && err.receipt?.status === "reverted";
        if (reverted && f.hash) await this.refuse(world, day, String(err));
        else {
          a.lastAction = `${this.label()}-pending`; a.lastError = String(err).slice(0, 200);
          await this.event(world, day, "pending", { reason: a.lastError, transaction_hash: f.hash });
        }
        return true;
      }
      if (!await this.applyKnown(world, day, result, ctr)) return true;
    }
    f.status = "complete"; a.fundingPending = false;
    a.lastAction = f.kind === "wallet" ? `wallet-${f.plan.action}` : f.kind === "exit" ? `exit-${f.plan.step}`
      : f.kind === "invest" ? f.retained ? "invest-retained" : "invest"
      : f.kind === "surplus" ? f.plan.action === "retire" ? "retirement-funded"
        : `${f.plan.action === "hold" ? "buy" : f.plan.action}${f.retained ? "-retained" : ""}`
      : f.satisfied ? "refinance-satisfied" : "refinance";
    a.lastError = "";
    // Completion of a retire plan funds a target; principal changes only in
    // the selected-facility engine's own confirmed records.
    await this.event(world, day, "complete", f.kind === "surplus" ? { outcome: a.lastAction, retirement: this.retirement() }
      : ["wallet", "exit"].includes(f.kind) ? { outcome: a.lastAction }
      : f.kind === "invest" ? { outcome: a.lastAction, basket: await this.basket(world) } : {});
    return true;
  }
}
