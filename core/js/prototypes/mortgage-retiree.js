// The two-sided mortgage debtor, AUDITED: the JS port of the Python
// BuckCreditDebtorAgent, proven by the isolation-world ledger audit to
// exactness:  adv = interest_saved + jubilee - premium - trade_loss.
//
// What the audit taught, now doctrine here:
//   * credits carry a REAL premium, so Buck.mint's funding gate bites;
//     the SAVE leg buys the required BUCK buffer ahead of each tranche
//     (a revert is the gate saying "save more" -- counted, retried);
//   * the liability is the chain's own melting quote: drawn net of
//     Buck.reliefOf (~2%/yr of the lien; never force-closed);
//   * the unwind buys ONLY below USD par (the basket signal says nothing
//     about USD price) and never lifts the pool past par -- profitable.
//
// Plumbing lives in the world (pledgeInsured / creditState / buyBuck /
// gateShortfall / mintTranche / sellBuckCapped / unwindBite); the agent
// is its own accountant: ledger rows carry both trajectories plus the
// cost telemetry, so nw chart and conservation identity both fall out.

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
   * @param opts.aggrBp     tolerated basket discount, bp (theta * apr):
   *                        deploy while bvib <= 1 + aggrBp
   * @param opts.saveRate   pct of spare cash routed to the gate buffer
   * @param opts.retireDiscBp unwind when the pool spot is this far below par
   * @param opts.cashBuffer floor the unwind never spends into
   */
  constructor({ house, mortgageBp, premiumBp, payment, account,
                aggrBp = 0n, saveRate = 50n, retireDiscBp = 200n,
                cashBuffer = 20_000n * E6 }) {
    Object.assign(this, { house, mortgageBp, premiumBp, payment, account,
                          aggrBp, saveRate, retireDiscBp, cashBuffer });
    this.mortgageOwing = house;
    this.hypoOwing = house;   // the counterfactual: the mortgage untouched
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
    if (tick !== 0 || day % MONTH !== 0) return;
    let budget = this.payment;
    if (this.mortgageOwing > 0n) {
      const interest = this.mortgageOwing * this.mortgageBp / BP / 12n;
      const pay = min(budget, this.mortgageOwing + interest);
      this.mortgageOwing += interest - pay;
      budget -= pay;
    }
    this.usdc += budget;

    const cs = await world.creditState(this.account);
    const bvib = await world.session.call(world.basket, "basketValueInBuck");
    this.jub = min(cs.jub, cs.drawn);
    const spare = this.usdc > this.cashBuffer
      ? this.usdc - this.cashBuffer : 0n;
    const tranche = min(min(this.payment * 12n, cs.headroom),
                        this.mortgageOwing);
    const deployOk = bvib <= (E18 * (BP + this.aggrBp)) / BP;

    if (tranche >= E6 && this.mortgageOwing > E6 && deployOk) {
      // SAVE for the real gate, preferring cheap BUCK (deployOk window).
      const { shortfall } = await world.gateShortfall(this.account, tranche);
      const spend = min(shortfall, (spare * this.saveRate) / 100n);
      if (spend >= E6) {
        const got = await world.buyBuck(spend, this.account,
                                        { tag: `debtor:save:d${day}` });
        this.usdc -= spend;
        this.tradeLoss += spend - got;
      }
      // DEPLOY: activate a tranche through the gate, sell, retire.
      const m = await world.mintTranche(this.account,
        min(tranche, cs.unactivated), { tag: `debtor:mint:d${day}` });
      if (!m.ok) {
        this.throttled += 1;
      } else {
        this.premiumPaid += m.premium;
        const { sold, got } = await world.sellBuckCapped(tranche,
          this.account, { tag: `debtor:draw:d${day}` });
        this.tradeLoss += sold - got;
        this.mortgageOwing -= min(got, this.mortgageOwing);
      }
    } else if (this.mortgageOwing <= E6 && cs.drawn > 0n && spare >= E6) {
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

    const post = await world.creditState(this.account);
    this.ledger.push({ day, drawn: post.drawn, jub: this.jub,
                       premiumPaid: this.premiumPaid,
                       tradeLoss: this.tradeLoss,
                       mortgageOwing: this.mortgageOwing,
                       banked: this.usdc, hypoOwing: this.#hypoMonth() });
  }

  #hypoMonth() {
    // The counterfactual: the same payment against the untouched loan;
    // after payoff the payment banks, keeping net worths comparable.
    if (this.hypoOwing > 0n) {
      const interest = this.hypoOwing * this.mortgageBp / BP / 12n;
      const pay = min(this.payment, this.hypoOwing + interest);
      this.hypoOwing += interest - pay;
      this.hypoBanked += this.payment - pay;
    } else {
      this.hypoBanked += this.payment;
    }
    return this.hypoOwing;
  }
}
