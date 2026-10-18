// Portable policy, in whole currency units. Python: alberta_buck/sim/household_policy.py and carry.py.
export const DEFAULTS = {
  home_k: [200, 1500], ltv: [.05, .85], income_frac: [.20, .35], living_frac: [.55, .70],
  cash_k: [20, 60], apr: [.045, .075], premium_bp: [25, 100], theta: [.5, 3],
  risk: [0, .01], switch_k: [1, 3], penalty_months: 3, term_months: 60, renew_frac: [0, 1],
  retire_disc: [.005, .03], issue_premium: [.005, .025], utilization: [.65, .85],
  buffer_k: 10, max_impact_bp: [25, 100], decision_days: [5, 20], surplus_policy: "adaptive",
  role: "debtor", credit_invest: true,
  // Share of the K-scaled limit a household with no mortgage keeps drawn and invested while the BUCK
  // is not cheap (0: it borrows only at a premium).
  carry_utilization: 0,
  // The investor: opening savings and yearly saving ($k), and the years before it withdraws.
  invest_cash_k: [10, 100], invest_income_k: [2, 20], hold_years: [3, 15],
  // A debtor or saver sells its insured asset at this hazard a year, buying back its whole lien first.
  divest_rate: .1,
  // While the BUCK trades at a discount d, a household with a lien pays down
  // paydown_share x (2^(d / paydown_double) - 1) of it each decision (0: no paydown).
  paydown_share: .01, paydown_double: .005,
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

export function uniform(seed, member, key) {
  let h = 2166136261;
  for (const byte of new TextEncoder().encode(`${seed}:${member}:${key}`)) h = Math.imul(h ^ byte, 16777619) >>> 0;
  h ^= h >>> 16; h = Math.imul(h, 0x85ebca6b) >>> 0;
  h ^= h >>> 13; h = Math.imul(h, 0xc2b2ae35) >>> 0;
  h ^= h >>> 16;
  return ((h >>> 0) + .5) / 4294967296;
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
    "invest_cash_k", "invest_income_k", "hold_years", "divest_rate", "paydown_share", "paydown_double"]) {
    if (!Number.isFinite(p[key]) || p[key] < 0) throw new Error(`invalid household parameter: ${key}`);
  }
  if (p.home_k <= 0 || p.max_impact_bp > 10000 || p.renew_frac > 1) throw new Error("invalid household size, impact or renewal fraction");
  if (p.hold_years <= 0) throw new Error("an investor's hold_years must be positive");
  if (p.paydown_double <= 0) throw new Error("paydown_double must be positive");
  if (!POLICIES.includes(p.surplus_policy)) throw new Error("unknown household surplus_policy");
  if (!ROLES.includes(p.role)) throw new Error("unknown household role");
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
export const discount = (spot, basket, fee) => spot <= 0 || basket <= 0 ? 0
  : Math.max(0, 1 - spot / ((1 - fee) * basket));

// The share of its lien a household pays down this decision at discount d: compounding urgency.
export const paydown = (d, p) => d <= 0 || p.paydown_share <= 0 ? 0
  : Math.min(1, p.paydown_share * (2 ** (d / p.paydown_double) - 1));

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

export function refinance(t, usd, buck) {
  const e = t.premium_bp / 1000, spc = t.k * .99 - e;
  const need = Math.max(0, buck - t.headroom);
  const cov = need === 0 ? 0 : spc > 0 ? need / spc : Infinity;
  if (!Number.isFinite(cov)) return NO;
  const dep = cov * e, value = buck * t.buck_value, valueDep = dep * t.buck_value;
  const parts = {
    interest: usd * t.rate, relief: (value + valueDep) * .02,
    premium: t.joined ? 0 : t.face * t.premium_bp / 10000,
    deposit_opp: -valueDep * (t.opp_rate ?? t.rate), deposit_age: -valueDep * .02,
    risk: -value * t.risk, execution: -(value - usd), fixed: -t.fixed_cost,
    penalty: -usd * t.rate * t.penalty_months / 12,
  };
  const carry = ["interest", "relief", "premium", "deposit_opp", "deposit_age", "risk"].reduce((s, k) => s + parts[k], 0);
  const one_time = -(parts.execution + parts.fixed + parts.penalty);
  const net = t.payback_y * carry - one_time;
  return { go: usd > 0 && carry > 0 && net > 0, usd, buck, coverage: cov, deposit: dep,
           carry, one_time, value: net, parts };
}

const capacity = (t) => t.headroom + t.unactivated * Math.max(0, t.k * .99 - t.premium_bp / 1000);
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
  if (policy === "basket") return "invest";
  if (policy === "retire") return "retire";
  if (spot / (1 - fee) < basket * (1 - discount)) return "retire";
  if (policy === "adaptive" && spot * (1 - fee) > basket * (1 + premium)) return "invest";
  return "hold";
}

export function premiumSale(cap, ru, rb, fee, basket, premium, impact) {
  if (Math.min(cap, ru, rb, basket) <= 0) return 0;
  const net = 1 - fee, floor = basket * (1 + premium);
  const budget = rb * (Math.sqrt(ru / rb * net / floor) - 1) / net;
  return Math.max(0, Math.min(cap, budget, rb * impact / 20000) * .999);
}

export function terms(s, p, investment = false) {
  return { debt: s.mortgage, rate: investment ? 0 : p.apr, face: s.face,
    premium_bp: p.premium_bp, joined: s.joined, payback_y: p.theta, k: s.k,
    headroom: s.spendable, unactivated: s.unactivated,
    fixed_cost: s.joined ? 0 : p.switch_k * 1000,
    penalty_months: investment || s.at_renewal ? 0 : p.penalty_months,
    risk: p.risk, opp_rate: p.apr, buck_value: s.basket_usd };
}

export function issuance(s, p) {
  const { ru, rb, fee } = s;
  let t = terms(s, p);
  if (s.mortgage > 1) {
    t.debt = Math.min(t.debt, usdcOut(rb * p.max_impact_bp / 20000, ru, rb, fee));
    return ["refinance", bestRefinance(t, ru, rb, fee)];
  }
  if (!p.credit_invest || ["retire", "discount"].includes(p.surplus_policy)) return ["hold", NO];
  t = terms(s, p, true);
  const limit = s.face * s.k * p.utilization - s.drawn;
  let buck = premiumSale(Math.min(capacity(t), Math.max(0, limit)), ru, rb, fee,
                         s.basket_usd, p.issue_premium, p.max_impact_bp);
  const keep = s.face * s.k * p.carry_utilization - s.drawn;
  const cheap = ru / rb / (1 - fee) < s.basket_usd * (1 - p.retire_disc);
  // Carry: keep this share of the K-scaled limit drawn and invested while the BUCK is not cheap.
  if (buck <= 0 && keep > 0 && !cheap) buck = Math.max(0, Math.min(capacity(t), keep, rb * p.max_impact_bp / 20000) * .999);
  return ["invest", refinance(t, usdcOut(buck, ru, rb, fee), buck)];
}
