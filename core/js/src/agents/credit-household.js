// The same household as alberta_buck/sim/credit_households.py. No independent income agent is needed.
import { profile, uniform, issuance, surplusAction, discountedBuyback, usdcOut, investorAction, population,
  arrivalState, arrivalsStep, divestDay, discount, paydown, moneyTarget, ROLES } from "./household-policy.js";

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
    // An investor brings dollars and a yearly saving; no insured asset, mortgage or cash buffer.
    this.investor = p.role === "investor";
    this.face = this.investor ? 0n : units(p.home_k * 1000);
    this.mortgage = p.role === "debtor" ? BigInt(Math.trunc(Number(this.face) * p.ltv)) : 0n;
    this.hypoMortgage = this.mortgage;
    this.incomeAnnual = this.investor ? units(p.invest_income_k * 1000)
      : BigInt(Math.trunc(Number(this.face) * p.income_frac));
    const rate = p.apr / 12;
    this.payment = rate ? BigInt(Math.trunc(Number(this.mortgage) * rate / (1 - (1 + rate) ** -300))) : this.mortgage / 300n;
    this.buffer = this.investor ? 0n : units(p.buffer_k * 1000);
    this.exitDay = this.investor ? arrive_day + Math.trunc(365 * p.hold_years) : null;
    this.divestDay = this.investor ? null : divestDay(seed, member, arrive_day, p.divest_rate);
    this.walletBought = 0n;     // BUCKs bought for the working balance
    this.sold = false;          // the insured asset has been sold
    this.released = false;     // its coverage has been released
    this.exited = false;
    this.withdrawnUsd = 0n;
    this.refusals = 0;
    this.book = new Map();                  // receipt -> {principal, shares}, taken off at exit
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
    this.fundingPending = false;
  }

  get mortgageOwing() { return this.mortgage; }
  get hypoOwing() { return this.hypoMortgage; }
  get hypoBanked() { return this.hypoCash; }

  async setup(world) {
    this.tokenIdx = Math.trunc(uniform(this.seed, this.member, "token") * world.tokens.length);
    if (this.investor) { await world.proxyFor(this.account); return; }   // its dollars arrive at the proxy
    const per = this.face / 4n;
    await world.pledgeInsured(this.account, [per, per, per, this.face - 3n * per], BigInt(this.p.premium_bp),
      { tag: "household:pledge" });
  }

  async monthly(world) {
    const ops = world.households, income = this.incomeAnnual / 12n;
    await world.fiatIn(this.account, income, { tag: "household:income" });
    this.incomeTotal += income; this.hypoCash += income;
    const living = this.investor ? 0n : BigInt(Math.trunc(Number(income) * this.p.living_frac));
    this.livingArrears += living;
    let paid = await ops.pay(this.account, this.livingArrears, "household:living");
    this.livingArrears -= paid; this.livingPaid += paid;
    this.hypoLivingArrears += living;
    paid = min(this.hypoLivingArrears, this.hypoCash);
    this.hypoLivingArrears -= paid;
    this.hypoCash -= paid; this.hypoLiving += paid;
    const premium = this.sold ? 0n : this.face * BigInt(this.p.premium_bp) / 10000n / 12n;
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
    await this.tendWallet(world);
  }

  // Payday: bring the BUCK working balance to its target (credit_households._tend_wallet).
  async tendWallet(world) {
    const spending = whole(this.incomeAnnual) * this.p.living_frac;
    if (this.investor || this.sold || spending <= 0 || this.p.money_months <= 0) return;
    const ops = world.households, st = await ops.read(this.account);
    const target = units(moneyTarget(this.lastDay - this.arrive_day, spending, discount(st.ru / st.rb, st.basket_usd, st.fee), this.p));
    const r = await ops.tendWallet(this, target);
    this.tradeLoss += r.spent - r.got; this.walletBought += r.got;
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
      this.book.set(r.rid, { principal: r.principal, shares: r.shares });
    }
    if (r.error) this.lastError = r.error;
  }

  async issue(world, state, kind, v) {
    // Reassess each decision. Preserve USDC fees and accumulated gate BUCKs before saving.
    this.fundingPending = v.go && v.buck >= 1;
    if (!v.go || v.buck < 1) return;
    const keep = kind === "invest" && this.p.surplus_policy === "basket";   // borrowed to save: deposit the BUCKs
    const r = await world.households.issue(this.account, state, v, this.buffer, this.p.premium_bp, { sell: !keep });
    if (keep) {
      this.tradeLoss += r.gateSpent - r.gateGot; this.premiumPaid += r.premium;
      if (r.error) this.lastError = r.error;
      if (r.reason === "gate-wait" || r.reason === "mint-wait") this.throttled += 1;
      if (!r.kept || r.kept < E6) { this.lastAction = r.reason; return; }
      const saved = await world.households.saveBucks(this, 0n, r.kept);
      this.book_(saved.deposit);
      this.fundingPending = false;
      this.switchPaid += await world.households.pay(this.account, units(-v.parts.fixed), "household:switch");
      this.joined = true; this.deploys += 1; this.issuedInvest += r.kept; this.lastAction = kind;
      return;
    }
    this.tradeLoss += r.gateSpent - r.gateGot + r.sold - r.got;
    this.premiumPaid += r.premium;
    this.lastAction = r.reason;
    if (r.error) this.lastError = r.error;
    if (r.reason === "gate-wait" || r.reason === "mint-wait") this.throttled += 1;
    if (!r.sold) return;
    this.fundingPending = false;
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
    if (this.fundingPending) { this.lastAction = "fund-issue"; return; }
    const p = this.p, { ru, rb, fee } = state;
    const spare = max(0n, await world.households.cash(this.account) - this.buffer);
    if (p.surplus_policy === "basket") { await this.saveOrPayDown(world, state, spare); return; }
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
      await this.redeemToRetire(world);
    }
    this.lastAction = action;
  }

  // The other half of borrowing to invest: while the BUCK is cheap, redeem the oldest basket receipt (an
  // equity basket pays exits in BUCKs) and repay the lien with what comes back; the excess stays saved.
  async redeemToRetire(world) {
    if (!this.receipts.length || !this.savings || world.basketKind !== "machine") return null;
    const r = await world.households.redeemToRetire(this, this.receipts[0]);
    if (r.refused) { this.refusals += 1; return null; }
    if (!r.redeemed) return null;
    const rid = this.receipts.shift(), b = this.book.get(rid) ?? { principal: 0n, shares: 0n };
    this.book.delete(rid);
    this.basketPrincipal -= b.principal; this.shares -= b.shares; this.unwound += r.repaid;
    return r.repaid;
  }

  // Record a BUCK deposit's receipt (the result of households.depositBuck).
  book_(r) {
    if (!r) return;
    this.refusals += r.refusals ?? 0;
    if (r.rid === undefined) return;
    this.receipts.push(r.rid); this.basketPrincipal += r.principal; this.shares += r.shares;
    this.book.set(r.rid, { principal: r.principal, shares: r.shares });
  }

  // The basket policy (credit_households._save_or_pay_down): while the BUCK trades at a discount, pay down
  // the lien with an urgency compounding with the discount, basket shares first, then spare dollars;
  // otherwise save, buying BUCKs and depositing them in the basket.
  async saveOrPayDown(world, state, spare) {
    const ops = world.households, p = this.p;
    const share = paydown(discount(state.ru / state.rb, state.basket_usd, state.fee), p);
    const drawn = -(await ops.read(this.account)).signed;
    if (share > 0 && drawn > E6) {
      const target = BigInt(Math.trunc(Number(drawn) * share));
      let repaid = 0n;
      while (repaid < target) {
        const got = await this.redeemToRetire(world);
        if (got === null) break;
        repaid += got;
      }
      if (repaid < target && spare > E6) {
        const want = whole(target - repaid), { ru, rb, fee } = state;
        const cost = want < rb / 2 ? units(want * ru / ((rb - want) * (1 - fee))) : spare;
        const r = await ops.buy(this.account, min(spare, cost, units(ru * p.max_impact_bp / 20000)));
        this.tradeLoss += r.spent - r.got; this.unwound += r.got;
      }
      this.lastAction = "pay-down";
      return;
    }
    const saved = await ops.saveBucks(this, spare);
    this.investedUsd += saved.spent; this.tradeLoss += saved.spent - saved.got;
    this.book_(saved.deposit);
    this.lastAction = "save";
  }

  // The investor holds BUCKs as money: it buys them with the dollars saved, unless they are dear; then it
  // converts those held into basket shares. At the horizon it withdraws everything.
  async investorDecision(world, day) {
    const ops = world.households;
    if (day >= this.exitDay) { this.lastAction = "exit"; await this.withdraw(world); return; }
    const st = await this.state(world, day);
    const action = investorAction(st.ru / st.rb, st.basket_usd, st.fee, this.p.issue_premium);
    if (action === "buy") {
      const cash = await ops.cash(this.account), budget = min(cash, units(st.ru * this.p.max_impact_bp / 20000));
      if (budget > E6) {
        const r = await ops.buy(this.account, budget);
        this.tradeLoss += r.spent - r.got; this.investedUsd += r.spent;
      }
    } else await this.depositBucks(world);
    this.lastAction = action;
  }

  async depositBucks(world) {
    const r = await world.households.depositBuck(this);
    this.refusals += r.refusals ?? 0;                 // refused BUCKs stay; the next decision tries again
    if (r.rid !== undefined) {
      this.receipts.push(r.rid); this.basketPrincipal += r.principal; this.shares += r.shares;
      this.book.set(r.rid, { principal: r.principal, shares: r.shares });
    }
  }

  async withdraw(world) {
    const r = await world.households.withdraw(this);
    for (const rid of r.redeemed) {
      const b = this.book.get(rid) ?? { principal: this.basketPrincipal, shares: this.shares };
      this.book.delete(rid);
      this.receipts = this.receipts.filter((x) => x !== rid);
      this.basketPrincipal -= b.principal; this.shares -= b.shares;
    }
    if (r.refused) this.refusals += 1;
    this.withdrawnUsd += r.usd;
    if (!r.left && !this.receipts.length) this.exited = true;
  }

  // Sell the insured asset (household_policy and credit_households._divest): its price pays any mortgage
  // left; the basket shares are redeemed toward the lien; the rest is bought back a capped bite a day;
  // the coverage is released and the BUCKs left are sold; then the household leaves. The counterfactual
  // sells the same asset on the same day.
  async divest(world) {
    const ops = world.households;
    if (!this.sold) {
      this.sold = true;
      await world.fiatIn(this.account, this.face, { tag: "household:sale" });
      this.hypoCash += this.face;
      let paid = await ops.pay(this.account, this.mortgage, "household:retire-external");
      this.mortgage -= paid; this.externalPaid += paid;
      paid = min(this.hypoMortgage, this.hypoCash);
      this.hypoCash -= paid; this.hypoMortgage -= paid; this.hypoPaid += paid;
      await ops.emptyWallet(this);    // the working balance goes to the lien first
    }
    const r = await ops.divest(this);
    for (const rid of r.redeemed ?? []) {
      const b = this.book.get(rid) ?? { principal: 0n, shares: 0n };
      this.book.delete(rid);
      this.receipts = this.receipts.filter((x) => x !== rid);
      this.basketPrincipal -= b.principal; this.shares -= b.shares;
    }
    this.unwound += r.bought ?? 0n;
    if (r.refused) this.refusals += 1;
    if (r.done) this.exited = true;
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
      fundingPending: this.fundingPending, exited: this.exited, withdrawnUsd: this.withdrawnUsd,
      wallet: s.wallet, walletBought: this.walletBought,
      refusals: this.refusals,
      error: this.lastError, decision: this.lastVerdict });
  }

  async act(world, day, tick) {
    if (tick || day < this.arrive_day || this.exited) return;
    if (!this.started) {
      const cash = units((this.investor ? this.p.invest_cash_k : this.p.cash_k) * 1000);
      await world.fiatIn(this.account, cash, { tag: "household:starting-cash" });
      this.hypoCash = cash; this.started = true;
    }
    await this.advance(world, day);
    if (this.divestDay !== null && day >= this.divestDay) {
      // Selling the asset is worked every day until the lien is cleared and the household leaves.
      this.lastError = ""; this.lastAction = "divest";
      try { await this.divest(world); } catch (e) { this.lastError = String(e).slice(0, 200); }
    } else if (day >= this.nextDecision) {
      this.nextDecision += (Math.floor((day - this.nextDecision) / this.p.decision_days) + 1) * this.p.decision_days;
      this.lastError = "";
      if (this.investor) {
        try { await this.investorDecision(world, day); } catch (e) { this.lastError = String(e).slice(0, 200); }
      } else try {
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

export class HouseholdInvestorAgent extends CreditHouseholdAgent {
  constructor(opts) { super({ ...opts, settings: { ...opts.settings, role: "investor" } }); }
}

const ROLE_CLASSES = { debtor: CreditHouseholdAgent, saver: CreditSaverAgent, investor: HouseholdInvestorAgent };

// A population whose households arrive while the world runs (household_policy.arrivals_step). At each
// day's tick 0 it reads the basket's price in BUCKs, adds the arrivals, then lets every member act.
// Population settings (POPULATION, or the older initial / arrival_rate / saver_share) are top-level
// options; `settings` go to every member. Members are named <population>/<role>/<k>.
export class HouseholdPopulationAgent {
  constructor({ makeAccount, seed = 0xA1BC, population: pop = 0, days = 1827, settings = {}, ...given }) {
    Object.assign(this, { makeAccount, seed, name: String(pop), days, settings });
    this.pop = population(given);
    this.state = arrivalState(seed, this.name);
    this.members = [];
    this.opened = false;
  }
  async setup(world) { this.world = world; }
  async arrive(world, role, k, day) {
    if (this.members.length >= this.pop.max_members) throw new Error("household arrivals exceed max_members; raise the bound or lower the rates");
    const member = `${this.name}/${role}/${k}`;
    const agent = new ROLE_CLASSES[role]({ account: this.makeAccount(member), seed: this.seed, member,
      arrive_day: day, settings: this.settings });
    await agent.setup(world);
    this.members.push(agent);
  }
  async morning(world, day) {
    if (!this.opened) {
      this.opened = true;
      for (const role of ROLES) for (let k = 0; k < this.pop[`initial_${role}s`]; k++) await this.arrive(world, role, k, day);
    }
    const bvib = day >= this.pop.settle_days ? Number(await world.bvib()) / 1e18 : null;
    const kNow = Number(await world.K()) / 1e18;
    for (const [role, k] of arrivalsStep(this.state, this.seed, this.name, day, this.days, bvib, kNow, this.pop)) {
      await this.arrive(world, role, this.pop[`initial_${role}s`] + k, day);
    }
  }
  async act(world, day, tick) {
    if (!tick) await this.morning(world, day);
    for (const a of this.members) await a.act(world, day, tick);
  }
  get summary() {
    const out = { average: this.state.average, gap: this.state.gap, multiplier: { ...this.state.multiplier } };
    for (const role of ROLES) {
      const mine = this.members.filter((a) => a.p.role === role);
      out[role] = { arrived: mine.length, active: mine.filter((a) => a.started && !a.exited).length,
                    exited: mine.filter((a) => a.exited).length };
    }
    return out;
  }
  get ledger() { return this.members.map((a) => ({ member: a.member, arrive_day: a.arrive_day, rows: a.ledger })); }
}
