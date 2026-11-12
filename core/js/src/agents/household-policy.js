// Portable policy, in whole currency units. Python: alberta_buck/sim/household_policy.py and carry.py.
import { netPurchaseExpected, netPurchaseCost, netSaleExpected } from "../net-market-quote.js";
export const DEFAULTS = {
  home_k: [200, 1500], ltv: [.05, .85], income_frac: [.20, .35], living_frac: [.55, .70],
  cash_k: [20, 60], apr: [.045, .075], premium_bp: [25, 100], theta: [.5, 3],
  risk: [0, .01], switch_k: [1, 3], penalty_months: 3, term_months: 60, renew_frac: [0, 1],
  retire_disc: [.005, .03], issue_premium: [.005, .025], utilization: [.65, .85],
  buffer_k: 10, max_impact_bp: [25, 100], decision_days: [5, 20], surplus_policy: "adaptive",
  role: "debtor", credit_invest: true, asset_count: 1,
  // Each household is paid on its own fixed day of the 30-day month, counted from the simulation's
  // day 0: by default a keyed random one, so wages, bills, instalments and working money do not
  // fall on the same day for a whole cohort. A whole number 0..29 pins it.
  pay_day: [0, 30],
  // Share of the K-scaled limit a household with no mortgage keeps drawn and invested while the BUCK
  // is not cheap (0: it borrows only at a premium).
  carry_utilization: 0,
  // The investor: opening savings and yearly saving ($k), and the years before it withdraws.
  invest_cash_k: [10, 100], invest_income_k: [2, 20], hold_years: [3, 15],
  // A debtor or saver sells its insured asset at this hazard a year, buying back its whole lien first.
  divest_rate: .1,
  // While the BUCK trades at a discount d, a household with a lien pays down
  // paydown_share x (2^(d / paydown_double) - 1) of it each decision (0: no paydown). Otherwise it saves
  // its spare dollars in BUCK deposits, the share halving each paydown_double of premium.
  paydown_share: .01, paydown_double: .005,
  // Money: a household keeps s x money_months of its spending in a BUCK wallet apart from its lien; s
  // ramps from adopt_start to 1 over adopt_years; it holds more while the BUCK is cheap.
  money_months: 1, adopt_start: .1, adopt_years: 1, money_gain: 200, money_cap: 8,
};
export const POLICIES = ["adaptive", "basket", "retire", "discount"];
export const ROLES = ["debtor", "saver", "investor"];
export const SUPPLY = ["debtor", "saver"];  // drawn while the BUCK is dear; investors while it is cheap
export const GROWTH = ["steady", "rising", "falling"];
export const POPULATION = {
  initial_debtors: 5, initial_savers: 5, initial_investors: 0, debtor_rate: 3, saver_rate: 3, investor_rate: 0,
  growth: "steady", gain: 50, gain_cap: 4, signal_halflife_days: 30, settle_days: 7, max_members: 2000,
  // Issuers arrive faster as K rises, by (1 - k_ref) / (1 - K).
  k_ref: .75, k_pace: true,
};
// Older settings, still accepted when no role is named: a total split between savers and debtors.
export const LEGACY_POPULATION = { initial: 10, arrival_rate: 6, saver_share: .5 };

// Raw-unit successor for canonical saving/discount buyback. PRE: caller has
// protected bills/current credit capital in cashFloor and pinned the market.
// POST: exact budgets/output bounds include carried fees and trading impact.
// A target is an intention; zero purchase cash may still permit LP redemption.
export function netSurplusPurchase(market, cash, issued, fairNum, fairDen, cashFloor, p, { slippage_bp = 50 } = {}) {
  const MAX = (1n << 256n) - 1n, WAD = 10n ** 18n, PPB = 1_000_000_000n;
  if ([cash, issued, cashFloor].some((v) => typeof v !== "bigint" || v < 0n || v > MAX)
      || [fairNum, fairDen].some((v) => typeof v !== "bigint" || v <= 0n)
      || !Number.isSafeInteger(slippage_bp) || slippage_bp < 0 || slippage_bp > 1000 || !POLICIES.includes(p.surplus_policy)) {
    throw new Error("invalid net surplus inputs");
  }
  netPurchaseExpected(market, 0n); // share the SDK's market validation, including empty net value
  const min = (a, b) => a < b ? a : b, max = (a, b) => a > b ? a : b;
  const spare = max(0n, cash - cashFloor);
  let cap = min(spare, market.reserve_usdc * BigInt(Math.trunc(p.max_impact_bp * 50)) / 1_000_000n);
  const out = { action: "hold", spend_usdc: 0n, minimum_net: 0n, expected_net: 0n, target_principal: 0n,
    cash_floor: cashFloor, reason: "no-net-delivery", marginal_ratio_wad: null };
  if (!market.net_scaled) return out;
  const ratio = market.price_num * market.gross_scaled * 1_000_000n * fairDen * WAD
    / (market.price_den * market.net_scaled * (1_000_000n - market.fee_ppm) * fairNum);
  const d = Number(max(0n, WAD - ratio)) / 1e18, threshold = BigInt(Math.ceil(p.retire_disc * 1e9));
  const mode = p.surplus_policy;
  const share = mode === "retire" || mode === "discount" && d > p.retire_disc ? 1
    : mode === "adaptive" ? paydown(Math.max(0, d - p.retire_disc), p) : 0;
  const target = issued * BigInt(Math.trunc(share * 1e9)) / PPB;
  const action = target ? "retire" : ["adaptive", "basket"].includes(mode) ? "save" : "hold";
  Object.assign(out, { action, target_principal: target, marginal_ratio_wad: ratio, reason: "no-spare-cash" });
  if (action === "hold") { out.reason = "no-retirement-opportunity"; return out; }
  if (action === "save" && mode === "adaptive" && cap) {
    const expected = netPurchaseExpected(market, cap);
    const averageRatio = expected ? cap * fairDen * WAD / (expected * fairNum) : WAD * 1_000_000n;
    const q = Number(min(WAD * 1_000_000n, max(0n, averageRatio - WAD))) / 1e18;
    cap = cap * BigInt(Math.trunc(saving(q, p) * 1e9)) / PPB;
  }
  if (action === "retire" && cap) {
    const cost = netPurchaseCost(market, target);
    if (cost !== null) cap = min(cap, cost);
  }
  const sizes = [...new Set([20n, 15n, 10n, 5n, 2n].map((f) => cap * f / 20n))].sort((a, b) => a > b ? -1 : a < b ? 1 : 0);
  for (const spend of sizes) {
    if (!spend) continue;
    const expected = netPurchaseExpected(market, spend);
    let minimum = expected * BigInt(10000 - slippage_bp) / 10000n;
    if (action === "retire" && mode !== "retire") {
      if (threshold >= PPB) continue;
      minimum = max(minimum, spend * fairDen * PPB / (fairNum * (PPB - threshold)) + 1n);
    }
    if (minimum < 1_000_000n || minimum > expected) continue;
    Object.assign(out, { spend_usdc: spend, minimum_net: minimum, expected_net: expected, reason: "ready" }); break;
  }
  return out;
}

/** Raw-unit working-balance choice from a pinned net market observation
 * (household_policy.net_wallet_adjustment). PRE: an Ordinary wallet in its own
 * custody; `target`/`held` are net BUCK there; the caller retained cashFloor.
 * POST: one capped bite: buy below 95% of the target, sell the excess above
 * 110%, else hold; integer minimums after carried/trading fees and impact;
 * nothing under one whole unit trades. Principal never changes. */
export function netWalletAdjustment(market, cash, held, target, cashFloor, p, { slippage_bp = 50 } = {}) {
  const MAX = (1n << 256n) - 1n, GRAIN = 1_000_000n;
  if ([cash, held, target, cashFloor].some((v) => typeof v !== "bigint" || v < 0n || v > MAX)
      || !Number.isSafeInteger(slippage_bp) || slippage_bp < 0 || slippage_bp > 1000) throw new Error("invalid net wallet inputs");
  const min = (a, b) => a < b ? a : b, impact = BigInt(Math.trunc(p.max_impact_bp * 50)), keep = BigInt(10000 - slippage_bp);
  const out = { action: "hold", spend_usdc: 0n, minimum_net: 0n, expected_net: 0n, sell_buck: 0n, minimum_usdc: 0n,
    expected_usdc: 0n, target, reason: "within-band" };
  if (held * 100n < target * 95n) {
    if (!market.net_scaled) return { ...out, reason: "no-net-delivery" };
    const spare = cash > cashFloor ? cash - cashFloor : 0n, cap = min(spare, market.reserve_usdc * impact / 1_000_000n);
    const cost = netPurchaseCost(market, target - held), spend = cost === null ? cap : min(cap, cost);
    const expected = netPurchaseExpected(market, spend), minimum = expected * keep / 10000n;
    if (spend < GRAIN || minimum < GRAIN) return { ...out, reason: cap < GRAIN ? "no-spare-cash" : "below-grain" };
    return { ...out, action: "buy", spend_usdc: spend, minimum_net: minimum, expected_net: expected, reason: "ready" };
  }
  if (held * 100n > target * 110n) {
    const bite = min(held - target, market.reserve_buck * impact / 1_000_000n);
    const expected = netSaleExpected(market, bite), minimum = expected * keep / 10000n;
    if (bite < GRAIN || minimum < GRAIN) return { ...out, reason: "below-grain" };
    return { ...out, action: "sell", sell_buck: bite, minimum_usdc: minimum, expected_usdc: expected, reason: "ready" };
  }
  return out;
}

/** Raw net BUCK working balance wanted at a marginal cost/fair ratio (1e18 is
 * parity; null, no quote). The discount is taken in parts per billion before
 * any float arithmetic, so every runtime sizes the same target. */
export function netWalletTarget(ratioWad, daysIn, annualSpending, p) {
  if (ratioWad !== null && (typeof ratioWad !== "bigint" || ratioWad < 0n)) throw new Error("invalid cost/fair ratio");
  const WAD = 10n ** 18n;
  const discount = ratioWad === null || ratioWad >= WAD ? 0 : Number((WAD - ratioWad) / 1_000_000_000n) / 1e9;
  return BigInt(Math.trunc(moneyTarget(daysIn, annualSpending, discount, p) * 1e6));
}

export function uniform(seed, member, key) {
  let h = 2166136261;
  for (const byte of new TextEncoder().encode(`${seed}:${member}:${key}`)) h = Math.imul(h ^ byte, 16777619) >>> 0;
  h ^= h >>> 16; h = Math.imul(h, 0x85ebca6b) >>> 0;
  h ^= h >>> 13; h = Math.imul(h, 0xc2b2ae35) >>> 0;
  h ^= h >>> 16;
  return ((h >>> 0) + .5) / 4294967296;
}

/** The first day strictly after arrival that is the household's day of the 30-day month
 * (household_policy.first_payday). Months are counted from the shared simulation day 0, so a
 * household keeps one payday for the whole run; the first comes 1 to 30 days after arrival. */
export function firstPayday(arriveDay, payDay) {
  if (!Number.isSafeInteger(arriveDay) || arriveDay < 0 || !Number.isSafeInteger(payDay) || payDay < 0 || payDay >= 30) {
    throw new Error("invalid arrival day or pay day");
  }
  return arriveDay + (((payDay - arriveDay - 1) % 30) + 30) % 30 + 1;
}

export function profile(seed, member, overrides = {}) {
  const p = {};
  for (const [key, setting] of Object.entries({ ...DEFAULTS, ...overrides })) {
    let value = setting;
    if (Array.isArray(value)) {
      if (value.length !== 2 || !value.every(Number.isFinite) || value[1] < value[0]) throw new Error(`invalid household range: ${key}`);
      const [lo, hi] = value, u = uniform(seed, member, key);
      if (key === "home_k" && lo <= 0) throw new Error("home_k must be positive");
      value = key === "home_k" ? lo * (hi / lo) ** u : lo + (hi - lo) * u;
    }
    p[key] = value;
  }
  for (const key of ["home_k", "ltv", "income_frac", "cash_k", "apr", "premium_bp", "theta", "risk", "switch_k",
    "penalty_months", "term_months", "renew_frac", "buffer_k", "max_impact_bp", "decision_days",
    "invest_cash_k", "invest_income_k", "hold_years", "divest_rate", "paydown_share", "paydown_double",
    "money_months", "adopt_start", "adopt_years", "money_gain", "money_cap"]) {
    if (!Number.isFinite(p[key]) || p[key] < 0) throw new Error(`invalid household parameter: ${key}`);
  }
  if (p.home_k <= 0 || p.max_impact_bp > 10000 || p.renew_frac > 1) throw new Error("invalid household size, impact or renewal fraction");
  if (p.hold_years <= 0) throw new Error("an investor's hold_years must be positive");
  if (p.paydown_double <= 0) throw new Error("paydown_double must be positive");
  if (p.adopt_start > 1 || p.adopt_years <= 0 || p.money_cap < 1) {
    throw new Error("invalid money settings: adopt_start <= 1, adopt_years > 0, money_cap >= 1");
  }
  if (!POLICIES.includes(p.surplus_policy)) throw new Error("unknown household surplus_policy");
  if (!ROLES.includes(p.role)) throw new Error("unknown household role");
  if (!Number.isSafeInteger(p.asset_count) || p.asset_count < 0 || p.asset_count > 32
      || p.role !== "investor" && p.asset_count === 0) {
    throw new Error("asset_count must be a whole number from 1 to 32 for insured households");
  }
  p.asset_count = p.role === "investor" ? 0 : p.asset_count;
  if (!Number.isFinite(p.pay_day) || p.pay_day < 0 || p.pay_day >= 30) throw new Error("pay_day must be a day of the 30-day month, 0 to 29");
  p.pay_day = Math.trunc(p.pay_day);
  if (!(p.retire_disc >= 0 && p.retire_disc < 1 && p.issue_premium >= 0)) throw new Error("invalid household price thresholds");
  if (!(p.utilization > 0 && p.utilization <= 1 && p.living_frac >= 0 && p.living_frac <= 1
    && p.carry_utilization >= 0 && p.carry_utilization <= 1)) {
    throw new Error("invalid household budget fractions");
  }
  p.decision_days = Math.max(1, Math.trunc(p.decision_days));
  p.phase = Math.trunc(uniform(seed, member, "phase") * p.decision_days);
  p.term_months = Math.max(1, Math.trunc(p.term_months));
  p.renew_in = Math.trunc(p.renew_frac * p.term_months);
  p.premium_bp = Math.trunc(p.premium_bp);
  return p;
}

const count = (value, key) => {
  if (!Number.isFinite(value) || !Number.isInteger(value) || value < 0) throw new Error(`${key} must be a whole number of households`);
  return value;
};

// The population's settings: those a world names, defaults for the rest. The older keys (initial,
// arrival_rate, saver_share) are read first when any is named; keys naming a role then override them.
export function population(given = {}) {
  given = Object.fromEntries(Object.entries(given).filter(([, v]) => v !== undefined && v !== null));
  const p = { ...POPULATION };
  if (Object.keys(LEGACY_POPULATION).some((k) => k in given)) {
    const old = { ...LEGACY_POPULATION, ...Object.fromEntries(Object.keys(LEGACY_POPULATION)
      .filter((k) => k in given).map((k) => [k, given[k]])) };
    const share = Number(old.saver_share);
    if (!(share >= 0 && share <= 1)) throw new Error("saver_share must be between zero and one");
    const initial = count(Number(old.initial), "initial"), rate = Number(old.arrival_rate);
    const savers = Math.floor(initial * share + .5);
    Object.assign(p, { initial_savers: savers, initial_debtors: initial - savers, saver_rate: rate * share,
                       debtor_rate: rate * (1 - share) });
  }
  for (const k of Object.keys(POPULATION)) if (k in given) p[k] = given[k];
  for (const role of ROLES) {
    p[`initial_${role}s`] = count(Number(p[`initial_${role}s`]), `initial_${role}s`);
    p[`${role}_rate`] = Number(p[`${role}_rate`]);
  }
  for (const k of ["gain", "gain_cap", "signal_halflife_days", "k_ref"]) p[k] = Number(p[k]);
  if (!(p.k_ref >= 0 && p.k_ref < 1)) throw new Error("k_ref must be in [0, 1)");
  p.k_pace = [true, 1, "true", "True"].includes(p.k_pace);
  p.settle_days = count(Number(p.settle_days), "settle_days");
  p.max_members = count(Number(p.max_members), "max_members");
  if (!GROWTH.includes(p.growth)) throw new Error(`growth must be one of ${GROWTH}`);
  if (!ROLES.every((r) => Number.isFinite(p[`${r}_rate`]) && p[`${r}_rate`] >= 0)
    || !(Number.isFinite(p.gain) && p.gain >= 0) || !(Number.isFinite(p.gain_cap) && p.gain_cap >= 1)
    || !(Number.isFinite(p.signal_halflife_days) && p.signal_halflife_days > 0)) {
    throw new Error("invalid household population rates, gain or averaging");
  }
  if (p.max_members < ROLES.reduce((s, r) => s + p[`initial_${r}s`], 0)) throw new Error("max_members is smaller than the opening population");
  return p;
}

// The base pace over the horizon: straight lines that each average one.
export function growthFactor(growth, day, days) {
  const t = days > 0 ? day / days : 0;
  return growth === "steady" ? 1 : growth === "rising" ? .25 + 1.5 * t : 1.75 - 1.5 * t;
}

const exponential = (seed, pop, role, k) => -Math.log(1 - uniform(seed, pop, `${role}/arrival/${k}`));

// Before day 0: the averaged basket price at parity, and each role's first arrival threshold.
export function arrivalState(seed, pop) {
  const each = (f) => Object.fromEntries(ROLES.map((r) => [r, f(r)]));
  return { average: 1, gap: 0, k_factor: 1, intensity: each(() => 0), arrived: each(() => 0),
           multiplier: each(() => 1), due: each((r) => exponential(seed, pop, r, 0)) };
}

// How much faster issuers arrive at this K than at k_ref: (1 - k_ref) / (1 - K).
export const kFactor = (k, p) => !p.k_pace || k === null || k === undefined || !Number.isFinite(k) ? 1
  : (1 - p.k_ref) / Math.max(1e-6, 1 - k);

// One day of arrivals, updating `state` in place; returns [[role, k], ...]. Mirrors
// household_policy.arrivals_step: the gap is 1 - the averaged basket price in BUCKs (positive while the
// BUCK is dear); debtors and savers arrive faster while it is dear and as K rises, investors while it
// is cheap.
export function arrivalsStep(state, seed, pop, day, days, bvib, k, p) {
  const settled = day >= p.settle_days;
  if (settled && bvib !== null && bvib !== undefined && Number.isFinite(bvib)) {
    state.average += (1 - .5 ** (1 / p.signal_halflife_days)) * (bvib - state.average);
  }
  const gap = settled ? 1 - state.average : 0;
  state.gap = gap;
  state.k_factor = kFactor(k, p);
  const pace = growthFactor(p.growth, day, days), arrived = [];
  for (const role of ROLES) {
    const side = SUPPLY.includes(role) ? gap : -gap;
    let m = Math.min(p.gain_cap, 1 + p.gain * Math.max(0, side));
    if (SUPPLY.includes(role)) m *= state.k_factor;
    state.multiplier[role] = m;
    state.intensity[role] += p[`${role}_rate`] * pace * m / 365.25;
    while (state.intensity[role] >= state.due[role]) {
      arrived.push([role, state.arrived[role]]);
      state.arrived[role] += 1;
      state.due[role] += exponential(seed, pop, role, state.arrived[role]);
    }
  }
  return arrived;
}

// How far below the basket's value a BUCK can be bought, after the fee (household_policy.discount).
/** Raw premium due on the uncovered nominal share at one common external
 * rate. Round remaining cost up; cash, debt and the joined flag do not enter. */
export function externalPremiumDue(fullPremium, face, activated) {
  const max = (1n << 256n) - 1n;
  if ([fullPremium, face, activated].some((v) => typeof v !== "bigint" || v < 0n || v > max)
      || activated > face || face === 0n && fullPremium !== 0n) throw new Error("invalid external insurance coverage");
  return face ? (fullPremium * (face - activated) + face - 1n) / face : 0n;
}

export const discount = (spot, basket, fee) => spot <= 0 || basket <= 0 ? 0
  : Math.max(0, 1 - spot / ((1 - fee) * basket));

// The share of its lien a household pays down this decision at discount d: compounding urgency.
export const paydown = (d, p) => d <= 0 || p.paydown_share <= 0 ? 0
  : Math.min(1, p.paydown_share * (2 ** (d / p.paydown_double) - 1));

// How far above the basket's value a BUCK costs, after the fee (household_policy.premium).
export const premium = (spot, basket, fee) => spot <= 0 || basket <= 0 ? 0
  : Math.max(0, spot / ((1 - fee) * basket) - 1);

// The share of its spare dollars a household saves at premium q: the paydown's mirror, halving each
// paydown_double of premium (household_policy.saving).
export const saving = (q, p) => 2 ** (-q / p.paydown_double);

// The BUCK working balance a household wants (household_policy.money_target), whole currency units.
export function moneyTarget(daysIn, annualSpending, d, p) {
  const share = Math.min(1, p.adopt_start + (1 - p.adopt_start) * daysIn / (365.25 * p.adopt_years));
  const held = share * annualSpending * p.money_months / 12;
  return held * Math.min(p.money_cap, 1 + p.money_gain * Math.max(0, d));
}

// The day a debtor or saver sells its insured asset: an exponential wait at `rate` a year, keyed by the
// member alone; null when it never does (household_policy.divest_day).
export const divestDay = (seed, member, arriveDay, rate) => rate <= 0 ? null
  : arriveDay + Math.trunc(-Math.log(1 - uniform(seed, member, "divest")) * 365.25 / rate);

// An investor holds BUCKs as its money: it buys them, unless they are dear by more than its premium
// after the fee; then it converts what it holds into basket shares (household_policy.investor_action).
export const investorAction = (spot, basket, fee, premium) => spot * (1 - fee) > basket * (1 + premium) ? "convert" : "buy";

export const usdcOut = (buck, ru, rb, fee) => rb + buck * (1 - fee) > 0
  ? ru * buck * (1 - fee) / (rb + buck * (1 - fee)) : 0;
export const buckIn = (usd, ru, rb, fee) => usd >= ru ? Infinity : usd * rb / ((ru - usd) * (1 - fee));
export const NO = Object.freeze({ go: false, usd: 0, buck: 0, coverage: 0, deposit: 0,
                                  carry: 0, one_time: 0, value: 0, parts: {} });

function validateForecast(reliefRate, refundFraction, refundGrowth, walletGrowth) {
  if (!Number.isFinite(reliefRate) || reliefRate < 0) throw new Error("invalid expected relief rate");
  if (!Number.isFinite(refundFraction) || refundFraction < 0 || refundFraction > 1) throw new Error("invalid premium refund fraction");
  if (!Number.isFinite(refundGrowth) || refundGrowth < -1) throw new Error("invalid expected refund growth");
  if (!Number.isFinite(walletGrowth) || walletGrowth < -1) throw new Error("invalid expected wallet growth");
}

function validateActivatedCapacity(value) {
  if (!Number.isFinite(value) || value < 0) throw new Error("invalid activated credit capacity");
}
function validateIssuanceLimit(value) {
  if (value !== null && value !== undefined && (!Number.isFinite(value) || value < 0)) {
    throw new Error("invalid shared issuance limit");
  }
}

export function refinance(t, usd, buck) {
  const reliefRate = t.expected_relief_rate ?? .02, refundFraction = t.premium_refund_fraction ?? 1;
  const refundGrowth = t.expected_refund_growth ?? -.02, walletGrowth = t.expected_wallet_growth ?? 0;
  validateForecast(reliefRate, refundFraction, refundGrowth, walletGrowth);
  const activatedCapacity = t.activated_capacity ?? 0;
  validateActivatedCapacity(activatedCapacity);
  validateIssuanceLimit(t.issuance_limit);
  const e = t.premium_bp / 1000, spc = t.k * .99 - e;
  const newSpendable = Math.max(0, buck - t.headroom);
  const need = Math.max(0, newSpendable - activatedCapacity);
  const cov = need === 0 ? 0 : spc > 0 ? need / spc : Infinity;
  if (!Number.isFinite(cov) || (t.issuance_limit != null && newSpendable + cov * e > t.issuance_limit)) return NO;
  const newlyIssued = t.wallet_already_issued ? Math.max(0, buck - t.headroom) : buck;
  const walletUsed = t.wallet_already_issued ? Math.min(buck, t.headroom) : 0;
  return scoreRefinance(t, usd, buck, cov, cov * e, newlyIssued, walletUsed);
}

function scoreRefinance(t, usd, buck, cov, dep, newlyIssued, walletUsed, reserveCosts = null, externalPremiumSaving = null) {
  const reliefRate = t.expected_relief_rate ?? .02, refundFraction = t.premium_refund_fraction ?? 1;
  const refundGrowth = t.expected_refund_growth ?? -.02, walletGrowth = t.expected_wallet_growth ?? 0;
  const value = buck * t.buck_value, valueDep = dep * t.buck_value;
  const refundValue = valueDep * refundFraction;
  const parts = {
    interest: usd * t.rate, relief: (newlyIssued * t.buck_value + valueDep) * reliefRate,
    // The legacy whole-asset switch is comparison-only. Canonical callers
    // pass only the annual USD saving on their newly activated coverage.
    premium: externalPremiumSaving ?? (t.wallet_already_issued || t.joined ? 0 : t.face * t.premium_bp / 10000),
    deposit_opp: -refundValue * (t.opp_rate ?? t.rate), deposit_age: refundValue * refundGrowth,
    deposit_write_down: -(valueDep - refundValue),
    wallet_carry: -walletUsed * t.buck_value * walletGrowth,
    risk: -value * t.risk, execution: -(value - usd), fixed: t.wallet_already_issued && newlyIssued === 0 ? 0 : -t.fixed_cost,
    penalty: -usd * t.rate * t.penalty_months / 12,
  };
  let carry = ["interest", "relief", "premium", "deposit_opp", "deposit_age", "wallet_carry", "risk"].reduce((s, k) => s + parts[k], 0);
  let one_time = -(parts.execution + parts.fixed + parts.penalty + parts.deposit_write_down);
  if (reserveCosts !== null) {
    parts.reserve_cost = -reserveCosts.cost;
    parts.reserve_carry = -reserveCosts.carry;
    one_time += reserveCosts.cost;
    carry -= reserveCosts.carry;
  }
  const net = t.payback_y * carry - one_time;
  return { go: usd > 0 && carry > 0 && net > 0, usd, buck, coverage: cov, deposit: dep,
           carry, one_time, value: net, parts };
}

/** Value a selected wallet/new-issue split. USD is the expected mortgage
 * principal actually retired after the trade. Coverage is its present marked
 * increment, not the face amount sent to the token. Reserve cost and carry
 * are incremental USD losses (or negative benefits), excluding the still
 * owned reserve and costs already in wallet opportunity. This float forecast
 * does not replace integer contract feasibility or a guarded market quote. */
export function refinanceQuoted(t, usd, walletUsed, newSpendable, presentCoverage, premium,
  { reserveCost = 0, reserveCarry = 0, premiumRefundFraction = null, expectedRefundGrowth = null,
    externalPremiumSaving = 0 } = {}) {
  if ((premiumRefundFraction !== null && !Number.isFinite(premiumRefundFraction))
      || (expectedRefundGrowth !== null && !Number.isFinite(expectedRefundGrowth))) {
    throw new Error("invalid quoted refund forecast");
  }
  t = { ...t, premium_refund_fraction: premiumRefundFraction ?? t.premium_refund_fraction,
    expected_refund_growth: expectedRefundGrowth ?? t.expected_refund_growth };
  if (!t.wallet_already_issued || t.expected_relief_rate == null || t.expected_refund_growth == null
      || [usd, walletUsed, newSpendable, presentCoverage, premium].some((v) => !Number.isFinite(v) || v < 0)
      || !Number.isFinite(reserveCost) || !Number.isFinite(reserveCarry)) {
    throw new Error("invalid quoted refinance inputs");
  }
  validateForecast(t.expected_relief_rate, t.premium_refund_fraction ?? 1, t.expected_refund_growth, t.expected_wallet_growth ?? 0);
  validateIssuanceLimit(t.issuance_limit);
  if (!Number.isFinite(t.debt) || t.debt < 0 || usd > t.debt) throw new Error("quoted refinance exceeds external debt");
  if (!Number.isFinite(t.headroom) || t.headroom < 0 || walletUsed > t.headroom) throw new Error("quoted refinance exceeds wallet funds");
  if (newSpendable === 0 && (presentCoverage || premium || reserveCost || reserveCarry)) {
    throw new Error("quoted refinance has issuance costs without issuance");
  }
  if (premium === 0 && (reserveCost || reserveCarry)) throw new Error("quoted refinance has reserve costs without a premium");
  if (t.issuance_limit == null || newSpendable + premium > t.issuance_limit) throw new Error("quoted refinance exceeds shared issuance limit");
  if (premium > presentCoverage) throw new Error("quoted refinance premium exceeds added coverage");
  if (!Number.isFinite(externalPremiumSaving) || externalPremiumSaving < 0
      || externalPremiumSaving > t.face * t.premium_bp / 10000
      || externalPremiumSaving && (!presentCoverage || !newSpendable)) {
    throw new Error("invalid incremental external premium saving");
  }
  return scoreRefinance(t, usd, walletUsed + newSpendable, presentCoverage, premium, newSpendable, walletUsed,
    { cost: reserveCost, carry: reserveCarry }, externalPremiumSaving);
}

/** Value one quoted issue whose BUCK is kept and invested, not sold to repay a
 * bank (carry.investment_quoted). `usd` is what the new BUCK would fetch at
 * the pinned dollar pool: execution value against its par liability, never
 * debt retired, so no external debt bounds it and no wallet money is spent.
 * `t` must be investment terms. Feasibility remains the caller's duty. */
export function investmentQuoted(t, usd, newSpendable, presentCoverage, premium,
  { reserveCost = 0, reserveCarry = 0, premiumRefundFraction = null, expectedRefundGrowth = null,
    externalPremiumSaving = 0 } = {}) {
  if ((premiumRefundFraction !== null && !Number.isFinite(premiumRefundFraction))
      || (expectedRefundGrowth !== null && !Number.isFinite(expectedRefundGrowth))) {
    throw new Error("invalid quoted refund forecast");
  }
  t = { ...t, premium_refund_fraction: premiumRefundFraction ?? t.premium_refund_fraction,
    expected_refund_growth: expectedRefundGrowth ?? t.expected_refund_growth };
  if (!t.wallet_already_issued || t.expected_relief_rate == null || t.expected_refund_growth == null
      || t.rate || t.penalty_months || !(newSpendable > 0)
      || [usd, newSpendable, presentCoverage, premium].some((v) => !Number.isFinite(v) || v < 0)
      || !Number.isFinite(reserveCost) || !Number.isFinite(reserveCarry)) {
    throw new Error("invalid quoted investment inputs");
  }
  validateForecast(t.expected_relief_rate, t.premium_refund_fraction ?? 1, t.expected_refund_growth, t.expected_wallet_growth ?? 0);
  validateIssuanceLimit(t.issuance_limit);
  if (premium === 0 && (reserveCost || reserveCarry)) throw new Error("quoted investment has reserve costs without a premium");
  if (t.issuance_limit == null || newSpendable + premium > t.issuance_limit) throw new Error("quoted investment exceeds shared issuance limit");
  if (premium > presentCoverage) throw new Error("quoted investment premium exceeds added coverage");
  if (!Number.isFinite(externalPremiumSaving) || externalPremiumSaving < 0
      || externalPremiumSaving > t.face * t.premium_bp / 10000 || externalPremiumSaving && !presentCoverage) {
    throw new Error("invalid incremental external premium saving");
  }
  return scoreRefinance(t, usd, newSpendable, presentCoverage, premium, newSpendable, 0,
    { cost: reserveCost, carry: reserveCarry }, externalPremiumSaving);
}

/** Choose among an explicit menu of already feasible candidates; do not infer
 * that capacity is monotone. Exact value ties prefer less coverage and then
 * less new issuance. This finite menu is not a proof of a global optimum. */
export function bestQuotedRefinance(t, candidates) {
  if (!Array.isArray(candidates)) throw new Error("invalid quoted refinance candidates");
  let index = null, verdict = NO, selected = null;
  candidates.forEach((candidate, i) => {
    const v = quotedVerdict(t, candidate);
    if (!v.go) return;
    if (selected === null || v.value > verdict.value || (v.value === verdict.value
        && (candidate.present_coverage < selected.present_coverage ||
            (candidate.present_coverage === selected.present_coverage && candidate.new_spendable < selected.new_spendable)))) {
      index = i; verdict = v; selected = candidate;
    }
  });
  return { index, verdict };
}

const quotedVerdict = (t, c) => refinanceQuoted(t, c.usd, c.wallet_used, c.new_spendable,
  c.present_coverage, c.premium,
  { reserveCost: c.reserve_cost ?? 0, reserveCarry: c.reserve_carry ?? 0,
    premiumRefundFraction: c.premium_refund_fraction ?? null, expectedRefundGrowth: c.expected_refund_growth ?? null,
    externalPremiumSaving: c.external_premium_saving ?? 0 });

/** The separately quoted programme pays the full startup cost in its score.
 * Allocate by principal only for the first leg's economics; its real bank
 * payment still includes the entire fixed cost. No future K/income/price is
 * assumed. The caller checks matching asset, exact quotes and cash bounds. */
export function stagedRefinance(t, tranche, programme) {
  const whole = quotedVerdict(t, programme), first = quotedVerdict(t, tranche);
  if (!whole.go || t.fixed_cost <= 0 || tranche.new_spendable <= 0
      || ["usd", "wallet_used", "new_spendable", "present_coverage", "premium"].some((key) => tranche[key] > programme[key])
      || (tranche.external_premium_saving ?? 0) > (programme.external_premium_saving ?? 0)) {
    return { verdict: NO, programme: whole };
  }
  return { verdict: quotedVerdict({ ...t, fixed_cost: t.fixed_cost * first.usd / whole.usd }, tranche), programme: whole };
}

const capacity = (t) => {
  const active = t.activated_capacity ?? 0, e = t.premium_bp / 1000, spc = Math.max(0, t.k * .99 - e);
  if (t.issuance_limit == null) return t.headroom + active + t.unactivated * spc;
  const used = Math.min(active, t.issuance_limit), totalPerCover = spc + e;
  const extra = spc > 0 && totalPerCover > 0 ? Math.min(t.unactivated, (t.issuance_limit - used) / totalPerCover) : 0;
  return t.headroom + used + extra * spc;
};
export function bestRefinance(t, ru, rb, fee) {
  if (t.debt <= 0 || capacity(t) <= 0 || ru <= 0 || rb <= 0) return NO;
  const usdMax = Math.min(t.debt, usdcOut(capacity(t) / 1.005, ru, rb, fee), .5 * ru);
  let best = NO;
  for (const f of [1, .75, .5, .25, .1]) {
    const usd = f * usdMax, buck = buckIn(usd, ru, rb, fee) * 1.005;
    if (!Number.isFinite(buck)) continue;
    const v = refinance(t, usd, buck);
    if (v.go && v.value > best.value) best = v;
  }
  return best;
}

export function discountedBuyback(cash, drawn, ru, rb, fee, basket, discount) {
  if (Math.min(cash, drawn, ru, rb, basket) <= 0 || !(fee >= 0 && fee < 1)) return 0;
  const net = 1 - fee, ceiling = basket * (1 - discount), spot = ru / rb;
  if (ceiling * net <= spot) return 0;
  const budget = ru * (Math.sqrt(ceiling * net / spot) - 1) / net;
  const spend = Math.min(cash, budget) * .999;
  return Math.min(drawn, rb * spend * net / (ru + spend * net));
}

export function surplusAction(policy, spot, basket, fee, discount, premium) {
  if (policy === "adaptive") return spot / (1 - fee) < basket ? "retire" : "invest";
  if (policy === "basket") return "invest";
  if (policy === "retire") return "retire";
  if (spot / (1 - fee) < basket * (1 - discount)) return "retire";
  return "hold";
}

export function premiumSale(cap, ru, rb, fee, basket, premium, impact) {
  if (Math.min(cap, ru, rb, basket) <= 0) return 0;
  const net = 1 - fee, floor = basket * (1 + premium);
  const budget = rb * (Math.sqrt(ru / rb * net / floor) - 1) / net;
  return Math.max(0, Math.min(cap, budget, rb * impact / 20000) * .999);
}

export function terms(s, p, investment = false) {
  const net = s.accounting === "net";
  if (net && ["expected_relief_rate", "premium_refund_fraction", "expected_refund_growth", "expected_wallet_growth", "activated_capacity", "issuance_limit"]
    .some((key) => s[key] === undefined || s[key] === null)) {
    throw new Error("net household needs explicit forecasts, activated credit capacity and issuance limit");
  }
  validateForecast(s.expected_relief_rate ?? .02, s.premium_refund_fraction ?? 1,
    s.expected_refund_growth ?? -.02, s.expected_wallet_growth ?? 0);
  const activatedCapacity = net ? s.activated_capacity : 0;
  validateActivatedCapacity(activatedCapacity);
  const issuanceLimit = net ? s.issuance_limit : null;
  validateIssuanceLimit(issuanceLimit);
  return { debt: s.mortgage, rate: investment ? 0 : p.apr, face: s.face,
    premium_bp: p.premium_bp, joined: s.joined, payback_y: p.theta, k: s.k,
    headroom: s.spendable, unactivated: s.unactivated,
    fixed_cost: (s.closing_paid ?? s.joined) ? 0 : p.switch_k * 1000,
    penalty_months: investment || s.at_renewal ? 0 : p.penalty_months,
    risk: p.risk, opp_rate: p.apr, buck_value: s.basket_usd,
    expected_relief_rate: s.expected_relief_rate, premium_refund_fraction: s.premium_refund_fraction ?? 1,
    expected_refund_growth: s.expected_refund_growth, wallet_already_issued: net,
    expected_wallet_growth: s.expected_wallet_growth ?? 0, activated_capacity: activatedCapacity, issuance_limit: issuanceLimit };
}

/** The BUCK a mortgage-free household would issue to invest now, in whole units, and why
 * (household_policy.investment_want): room that could be sold while the BUCK keeps a net premium of
 * issue_premium over basket value ("premium"); otherwise the carry line ("carry"), the share
 * carry_utilization of its K-scaled limit kept drawn and invested while the BUCK trades at no
 * discount -- the same line where paying down begins, so the two never alternate. */
export function investmentWant(s, p) {
  if (!p.credit_invest || ["retire", "discount"].includes(p.surplus_policy)) return [0, "hold"];
  const { ru, rb, fee } = s, t = terms(s, p, true);
  const limit = s.face * s.k * p.utilization - s.drawn;
  const buck = premiumSale(Math.min(capacity(t), Math.max(0, limit)), ru, rb, fee,
                           s.basket_usd, p.issue_premium, p.max_impact_bp);
  if (buck > 0) return [buck, "premium"];
  const keep = s.face * s.k * p.carry_utilization - s.drawn;
  if (keep > 0 && discount(ru / rb, s.basket_usd, fee) <= 0) {
    return [Math.max(0, Math.min(capacity(t), keep, rb * p.max_impact_bp / 20000) * .999), "carry"];
  }
  return [0, "hold"];
}

export function issuance(s, p) {
  const { ru, rb, fee } = s;
  const t = terms(s, p);
  if (s.mortgage > 1) {
    t.debt = Math.min(t.debt, usdcOut(rb * p.max_impact_bp / 20000, ru, rb, fee));
    return ["refinance", bestRefinance(t, ru, rb, fee)];
  }
  if (!p.credit_invest || ["retire", "discount"].includes(p.surplus_policy)) return ["hold", NO];
  const [buck] = investmentWant(s, p);
  return ["invest", refinance(terms(s, p, true), usdcOut(buck, ru, rb, fee), buck)];
}
