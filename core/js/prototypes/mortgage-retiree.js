// The two-sided mortgage debtor: the JS port of the Python
// BuckCreditDebtorAgent, whose ledger the isolation-world audit proved
// exact:  adv = interest_saved + jubilee - premium - trade_loss.
//
//   * a debtor does not pay interest it need not: each day it moves as
//     much of the loan onto its own credit as the market takes without
//     the price moving, until its credit is used or the loan is gone;
//   * credits carry a REAL premium, so Buck.mint's funding gate bites:
//     it finds the cash for its insurance deposit once, and buys the
//     BUCKs the gate asks ahead of each draw;
//   * the liability is the chain's own melting quote: drawn net of
//     Buck.reliefOf (~2%/yr of the lien; never force-closed);
//   * the unwind buys ONLY below USD par, and never lifts the pool past it.
//
// Plumbing lives in the world; the agent is its own accountant: ledger
// rows carry both trajectories and the cost telemetry.

const BP = 10_000n;
const MONTH = 30;
const E18 = 10n ** 18n;
const E6 = 10n ** 6n;
const min = (a, b) => (a < b ? a : b);

export class MortgageRetiree {
  /**
   * @param opts.house      insured-asset face == mortgage principal (6-dec)
   * @param opts.mortgageBp mortgage annual rate (e.g. 550n = 5.50%)
   * @param opts.premiumBp  REAL annual insurance premium (funding gate!)
   * @param opts.payment    fixed monthly payment / income, USDC base units
   * @param opts.account    funded account with a registered identity
   * @param opts.aggrBp     basket discount it still sells into, bp
   *                        (default: a year of the loan's interest)
   * @param opts.impactBp   most a day's sale may move the BUCK's price
   * @param opts.saveRate   pct of spare cash routed to the gate buffer
   * @param opts.retireDiscBp unwind when the pool spot is this far below par
   * @param opts.cashBuffer floor the unwind never spends into
   */
  constructor({ house, mortgageBp, premiumBp, payment, account,
                aggrBp = mortgageBp, impactBp = 50n, saveRate = 50n,
                retireDiscBp = 200n, cashBuffer = 20_000n * E6 }) {
    Object.assign(this, { house, mortgageBp, premiumBp, payment, account,
                          aggrBp, impactBp, saveRate, retireDiscBp, cashBuffer });
    this.mortgageOwing = house;
    this.hypoOwing = house;   // the counterfactual: the mortgage untouched
    this.days = 0n;           // each loan's daily balances summed since its
    this.hypoDays = 0n;       // last payment: interest is charged on these
    this.usdc = 0n;           // banked income awaiting the legs
    this.hypoBanked = 0n;
    this.premiumPaid = 0n;    // insurance principal drawn at each mint
    this.tradeLoss = 0n;      // par-value cost of crossing the pool (+/-)
    this.throttled = 0;       // funding-gate refusals
    this.jub = 0n;            // the chain's melting-liability quote
    this.ledger = [];
  }

  async setup(world) {
    const per = this.house / 4n;   // tranche-faced credits, real premium
    await world.pledgeInsured(this.account,
      [per, per, per, this.house - 3n * per], this.premiumBp,
      { tag: "debtor:pledge" });
  }

  async act(world, day, tick) {
    if (tick !== 0) return;
    const month = day % MONTH === 0;
    this.days += this.mortgageOwing; this.hypoDays += this.hypoOwing;
    if (month) {   // the payment falls due, on both loans alike
      const [owing, left] = this.#pay(this.mortgageOwing, this.days);
      const [hypo, hypoLeft] = this.#pay(this.hypoOwing, this.hypoDays);
      [this.mortgageOwing, this.hypoOwing, this.days, this.hypoDays] = [owing, hypo, 0n, 0n];
      this.usdc += left; this.hypoBanked += hypoLeft;
    }
    if (this.gateCash === undefined) {   // once, in both ledgers
      this.gateCash = await world.findDepositCash(this.account, this.mortgageOwing);
      this.usdc += this.gateCash; this.hypoBanked += this.gateCash;
    }

    const cs = await world.creditState(this.account);
    const bvib = await world.session.call(world.basket, "basketValueInBuck");
    this.jub = min(cs.jub, cs.drawn);
    const spare = this.usdc > this.cashBuffer ? this.usdc - this.cashBuffer : 0n;
    // Interest runs on what is left with the bank, so it takes all its
    // credit allows, a day's bite at a time, unless the BUCK is too cheap.
    const tranche = min(min(cs.headroom, this.mortgageOwing),
                        await world.sellBite(this.impactBp));

    if (tranche >= E6 && this.mortgageOwing > E6
        && bvib <= (E18 * (BP + this.aggrBp)) / BP) {
      // SAVE for the gate: its saving rate of spare cash, or the found cash.
      const save = (spare * this.saveRate) / 100n;
      const { tranche: bite, spend, got } = await world.buyForGate(this.account, tranche,
        save > this.gateCash ? save : this.gateCash, { tag: `debtor:save:d${day}` });
      this.usdc -= spend; this.tradeLoss += spend - got; this.gateCash -= min(spend, this.gateCash);
      // DEPLOY: activate what its cash got through the gate, sell, retire.
      const m = bite < E6 ? { wait: true } : await world.mintTranche(this.account, bite,
        { tag: `debtor:mint:d${day}` });
      if (!m.ok) {
        if (!m.wait) this.throttled += 1;
      } else {
        this.premiumPaid += m.premium;
        const { sold, got } = await world.sellBuckCapped(bite,
          this.account, { tag: `debtor:draw:d${day}` });
        this.tradeLoss += sold - got;
        this.mortgageOwing -= min(got, this.mortgageOwing);
      }
    } else if (month && this.mortgageOwing <= E6 && cs.drawn > 0n && spare >= E6) {
      // UNWIND, spot-gated + impact-capped: only below USD par, only
      // what cannot lift the pool past par.  Never urgent, never forced.
      const { spot, capBuck } = await world.unwindBite();
      if (spot <= (E6 * (BP - this.retireDiscBp)) / BP && capBuck >= E6) {
        const spend = min(spare, min(cs.drawn, capBuck) * spot / E6);
        if (spend >= E6) {
          const got = await world.buyBuck(spend, this.account,
                                          { tag: `debtor:unwind:d${day}` });
          this.usdc -= spend;
          this.tradeLoss += spend - got;
        }
      }
    }

    if (!month) return;
    const post = await world.creditState(this.account);
    this.ledger.push({ day, drawn: post.drawn, jub: this.jub,
                       premiumPaid: this.premiumPaid, tradeLoss: this.tradeLoss,
                       mortgageOwing: this.mortgageOwing, banked: this.usdc,
                       hypoOwing: this.hypoOwing, hypoBanked: this.hypoBanked });
  }

  /** One payment on a loan: interest on its daily balances since the last
   *  (30/360), then principal.  Returns [still owing, payment left over]. */
  #pay(owing, days) {
    const interest = days * this.mortgageBp / BP / 360n;
    const pay = min(this.payment, owing + interest);
    return [owing + interest - pay, this.payment - pay];
  }
}
