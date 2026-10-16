// The same household as alberta_buck/sim/credit_households.py. No independent income agent is needed.
import { profile, uniform, arrivals, issuance, surplusAction, discountedBuyback, usdcOut } from "./household-policy.js";

const E6 = 1_000_000n;
const units = (n) => BigInt(Math.trunc(n * 1e6));
const whole = (n) => Number(n) / 1e6;
const min = (...xs) => xs.reduce((a, b) => a < b ? a : b);
const max = (a, b) => a > b ? a : b;

export class CreditHouseholdAgent {
  constructor({ account, seed = 0xA1BC, member = "0", arrive_day = 0, settings = {} }) {
    Object.assign(this, { account, seed, member, arrive_day });
    this.p = profile(seed, member, settings);
    const p = this.p;
    this.face = units(p.home_k * 1000);
    this.mortgage = p.role === "debtor" ? BigInt(Math.trunc(Number(this.face) * p.ltv)) : 0n;
    this.hypoMortgage = this.mortgage;
    this.incomeAnnual = BigInt(Math.trunc(Number(this.face) * p.income_frac));
    const rate = p.apr / 12;
    this.payment = rate ? BigInt(Math.trunc(Number(this.mortgage) * rate / (1 - (1 + rate) ** -300))) : this.mortgage / 300n;
    this.buffer = units(p.buffer_k * 1000);
    this.nextPay = arrive_day + 30;
    this.lastDay = arrive_day;
    this.nextDecision = arrive_day + p.phase;
    this.started = false;
    this.joined = false;
    this.refis = 0;
    this.deploys = 0;
    this.throttled = 0;
    for (const k of ["hypoCash", "usdc", "premiumPaid", "extPremium", "hypoPremium", "switchPaid", "penaltyPaid",
      "externalInterest", "hypoInterest", "incomeTotal", "livingPaid", "hypoLiving", "tradeLoss", "unwound",
      "basketPrincipal", "shares", "investedUsd", "issuedInvest", "livingArrears", "hypoLivingArrears",
      "premiumArrears", "hypoPremiumArrears", "externalPaid", "hypoPaid"]) this[k] = 0n;
    this.receipts = [];
    this.ledger = [];
    this.lastAction = "waiting";
    this.lastIssue = "waiting";
    this.lastSurplus = "waiting";
    this.lastError = "";
    this.lastVerdict = null;
  }

  get mortgageOwing() { return this.mortgage; }
  get hypoOwing() { return this.hypoMortgage; }
  get hypoBanked() { return this.hypoCash; }

  async setup(world) {
    this.tokenIdx = Math.trunc(uniform(this.seed, this.member, "token") * world.tokens.length);
    const per = this.face / 4n;
    await world.pledgeInsured(this.account, [per, per, per, this.face - 3n * per], BigInt(this.p.premium_bp),
      { tag: "household:pledge" });
  }

  async monthly(world) {
    const ops = world.households, income = this.incomeAnnual / 12n;
    await world.fiatIn(this.account, income, { tag: "household:income" });
    this.incomeTotal += income; this.hypoCash += income;
    const living = BigInt(Math.trunc(Number(income) * this.p.living_frac));
    this.livingArrears += living;
    let paid = await ops.pay(this.account, this.livingArrears, "household:living");
    this.livingArrears -= paid; this.livingPaid += paid;
    this.hypoLivingArrears += living;
    paid = min(this.hypoLivingArrears, this.hypoCash);
    this.hypoLivingArrears -= paid;
    this.hypoCash -= paid; this.hypoLiving += paid;
    const premium = this.face * BigInt(this.p.premium_bp) / 10000n / 12n;
    if (!this.joined) this.premiumArrears += premium;
    paid = await ops.pay(this.account, this.premiumArrears, "household:insurance");
    this.premiumArrears -= paid; this.extPremium += paid;
    this.hypoPremiumArrears += premium;
    paid = min(this.hypoPremiumArrears, this.hypoCash);
    this.hypoPremiumArrears -= paid;
    this.hypoCash -= paid; this.hypoPremium += paid;
    paid = await ops.pay(this.account, min(this.payment, this.mortgage), "household:mortgage");
    this.mortgage -= paid; this.externalPaid += paid;
    paid = min(this.payment, this.hypoMortgage, this.hypoCash);
    this.hypoCash -= paid; this.hypoMortgage -= paid; this.hypoPaid += paid;
  }

  async advance(world, day) {
    while (this.lastDay < day) {
      const end = Math.min(day, this.nextPay), growth = (1 + this.p.apr / 365) ** (end - this.lastDay);
      const actual = BigInt(Math.trunc(Number(this.mortgage) * growth));
      const hypo = BigInt(Math.trunc(Number(this.hypoMortgage) * growth));
      this.externalInterest += actual - this.mortgage;
      this.hypoInterest += hypo - this.hypoMortgage;
      this.mortgage = actual; this.hypoMortgage = hypo; this.lastDay = end;
      if (end === this.nextPay) { await this.monthly(world); this.nextPay += 30; }
    }
  }

  async state(world, day) {
    const c = await world.households.read(this.account);
    const month = Math.floor((day - this.arrive_day) / 30);
    return { mortgage: whole(this.mortgage), face: whole(this.face), joined: this.joined,
      k: c.k, drawn: whole(c.drawn), spendable: whole(c.spendable), unactivated: whole(c.unactivated),
      ru: c.ru, rb: c.rb, fee: c.fee, basket_usd: c.basket_usd,
      at_renewal: ((month - this.p.renew_in) % this.p.term_months) === 0 };
  }

  async invest(world, budget) {
    const r = await world.households.invest(this, budget);
    this.investedUsd += r.spent;
    if (r.rid !== undefined) {
      this.receipts.push(r.rid); this.basketPrincipal += r.principal; this.shares += r.shares;
    }
    if (r.error) this.lastError = r.error;
  }

  async issue(world, state, kind, v) {
    if (!v.go || v.buck < 1) return;
    const r = await world.households.issue(this.account, state, v, this.buffer, this.p.premium_bp);
    this.tradeLoss += r.gateSpent - r.gateGot + r.sold - r.got;
    this.premiumPaid += r.premium;
    this.lastAction = r.reason;
    if (r.error) this.lastError = r.error;
    if (r.reason === "gate-wait" || r.reason === "mint-wait") this.throttled += 1;
    if (!r.sold) return;
    this.switchPaid += await world.households.pay(this.account, units(-v.parts.fixed), "household:switch");
    this.joined = true; this.deploys += 1;
    if (kind === "refinance") {
      const paid = await world.households.pay(this.account, min(r.got, this.mortgage), "household:retire-external");
      this.mortgage -= paid;
      this.externalPaid += paid;
      const penalty = state.at_renewal ? 0n : BigInt(Math.trunc(Number(paid) * this.p.apr * this.p.penalty_months / 12));
      this.penaltyPaid += await world.households.pay(this.account, penalty, "household:penalty");
      this.refis += 1;
    } else {
      this.issuedInvest += r.sold;
      await this.invest(world, r.got);
    }
    this.lastAction = kind;
  }

  async surplus(world, state) {
    const p = this.p, { ru, rb, fee } = state;
    const spare = max(0n, await world.households.cash(this.account) - this.buffer);
    const action = surplusAction(p.surplus_policy, ru / rb, state.basket_usd, fee, p.retire_disc, p.issue_premium);
    if (action === "invest") await this.invest(world, spare);
    else if (action === "retire" && state.drawn > 1) {
      const budget = Math.min(whole(spare), ru * p.max_impact_bp / 20000);
      const want = p.surplus_policy === "retire" ? Math.min(state.drawn, usdcOut(budget, rb, ru, fee))
        : discountedBuyback(budget, state.drawn, ru, rb, fee, state.basket_usd, p.retire_disc);
      if (want > 1) {
        const cost = units(want * ru / ((rb - want) * (1 - fee)));
        const r = await world.households.buy(this.account, min(units(budget), cost));
        this.tradeLoss += r.spent - r.got; this.unwound += r.got;
      }
    }
    this.lastAction = action;
  }

  async record(world, day) {
    const s = await world.households.mark(this);
    this.usdc = s.cash;
    this.ledger.push({ day, drawn: s.drawn, limit: s.limit, jub: min(s.jub, s.drawn),
      mortgageOwing: this.mortgage, hypoOwing: this.hypoMortgage, banked: s.cash, hypoBanked: this.hypoCash,
      nw: s.nw, hypo: this.hypoCash - this.hypoMortgage - this.hypoLivingArrears - this.hypoPremiumArrears,
      arrears: this.livingArrears + this.premiumArrears, hypoArrears: this.hypoLivingArrears + this.hypoPremiumArrears,
      externalPaid: this.externalPaid, hypoPaid: this.hypoPaid, issuedInvest: this.issuedInvest,
      insuranceAge: s.insuranceAge,
      basket: this.basketPrincipal, basketMark: s.basketMark,
      pending: s.pending, premiumPaid: this.premiumPaid, extPremium: this.extPremium, hypoPremium: this.hypoPremium,
      switchPaid: this.switchPaid, penaltyPaid: this.penaltyPaid, tradeLoss: this.tradeLoss, unwound: this.unwound,
      externalInterest: this.externalInterest, hypoInterest: this.hypoInterest, investedUsd: this.investedUsd,
      incomeTotal: this.incomeTotal, livingPaid: this.livingPaid, hypoLiving: this.hypoLiving,
      receipts: this.receipts.length, policy: this.p.surplus_policy, role: this.p.role,
      action: this.lastAction, issueAction: this.lastIssue, surplusAction: this.lastSurplus,
      error: this.lastError, decision: this.lastVerdict });
  }

  async act(world, day, tick) {
    if (tick || day < this.arrive_day) return;
    if (!this.started) {
      const cash = units(this.p.cash_k * 1000);
      await world.fiatIn(this.account, cash, { tag: "household:starting-cash" });
      this.hypoCash = cash; this.started = true;
    }
    await this.advance(world, day);
    if (day >= this.nextDecision) {
      this.nextDecision += (Math.floor((day - this.nextDecision) / this.p.decision_days) + 1) * this.p.decision_days;
      this.lastError = "";
      try {
        const state = await this.state(world, day), [kind, verdict] = issuance(state, this.p);
        this.lastVerdict = verdict;
        this.lastAction = verdict.go ? kind : "declined";
        await this.issue(world, state, kind, verdict);
        this.lastIssue = this.lastAction;
        await this.surplus(world, await this.state(world, day));
        this.lastSurplus = this.lastAction;
        if (this.savings) await this.invest(world, 0n);
      } catch (e) { this.lastError = String(e).slice(0, 200); }
    }
    await this.record(world, day);
  }
}

export class CreditSaverAgent extends CreditHouseholdAgent {
  constructor(opts) { super({ ...opts, settings: { ...opts.settings, role: "saver" } }); }
}

export class HouseholdPopulationAgent {
  constructor({ makeAccount, seed = 0xA1BC, population = 0, days = 1827, initial = 10,
                arrival_rate = 6, max_members = 256, saver_share = .5, settings = {} }) {
    if (!(saver_share >= 0 && saver_share <= 1)) throw new Error("saver_share must be between zero and one");
    this.members = arrivals(seed, population, days, initial, arrival_rate, max_members).map((arrive_day, i) => {
      const member = `${population}/${i}`;
      const cls = uniform(seed, member, "role") < saver_share ? CreditSaverAgent : CreditHouseholdAgent;
      return new cls({ account: makeAccount(member), seed, member, arrive_day,
        settings: { ...settings, role: cls === CreditSaverAgent ? "saver" : "debtor" } });
    });
  }
  async setup(world) { for (const a of this.members) await a.setup(world); }
  async act(world, day, tick) { for (const a of this.members) await a.act(world, day, tick); }
  get ledger() { return this.members.map((a) => ({ member: a.member, arrive_day: a.arrive_day, rows: a.ledger })); }
}
