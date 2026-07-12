// PROTOTYPE -- a structural exemplar, not yet wired to a world.
//
// The flagship example story for the JS platform (alberta-buck-platform.org,
// "The JS agent doctrine"), refined 2026-07-04 to the two-sided debtor:
// one screen, plain object, every send tagged, and the agent is its own
// accountant -- this.ledger carries the actual AND counterfactual
// trajectories, so the net-worth-vs-original-mortgage chart falls out:
//   actual net worth: house + banked - mortgageOwing - drawn
//   counterfactual:   house + hypoBanked - hypoOwing
//
// Runs on the equilibrium world (scenarios/eqworld.js) via its helpers:
//   world.pledge(account, face, {tag})     createCredit: headroom, no draw
//   world.sellBuck(buckIn, account, {tag}) -> USDC received (draws credit)
//   world.buyBuck(usdcIn, account, {tag})  -> BUCK received (retires draw)
//   world.usdcForBuck(buckOut)             -> USDC needed at current spot
//   world.holderAddress(account)           -> the on-chain position holder
//     (credit-drawers act through a public NON-carrying proxy the world
//     creates at pledge(); carrying accounts cannot draw negative)
// plus world.session / world.buck / world.basket contract handles.
//
// The debtor holds a USDC mortgage AND insured assets.  Day 0: pledge
// the assets for BuckCredit headroom (a draw costs no interest and no
// demurrage -- only the insurance premium; it is an outstanding claim
// on the debtor's own assets).  Monthly:
//   * service the mortgage with the fixed payment while it lasts; once
//     it is gone, the same payment banks as USDC savings.
//   * BUCK DEFLATION (basketValueInBuck < 1e18: BUCK above value): draw
//     by selling BUCK HIGH for USDC and retire mortgage principal early
//     -- swapping interest-bearing debt for the interest-free claim.
//   * BUCK INFLATION (> 1e18: BUCK below value): buy discounted BUCK
//     with banked USDC and retire the draw toward zero.
//   * forced deleverage stays real: if the controller tightens K past
//     the draw, buy BUCK at whatever the market asks -- booked as the
//     cost a credit-union analyst needs to see.

const BP = 10_000n;
const MONTH = 30;
const E18 = 10n ** 18n;
const BAND = E18 / 200n;   // 0.5% deadband: don't churn on parity noise
const min = (a, b) => (a < b ? a : b);

export class MortgageRetiree {
  /**
   * @param opts.house      insured-asset face == mortgage principal (6-dec)
   * @param opts.mortgageBp mortgage annual rate (e.g. 550n = 5.50%)
   * @param opts.premiumBp  annual insurance premium on the face (e.g. 50n)
   * @param opts.payment    fixed monthly mortgage payment, USDC base units
   * @param opts.account    funded account with a REAL registered identity
   * @param opts.recoverBp  voluntary overdraw-recovery effort, bp of the
   *                        excess bought back per month when K tightens
   *                        past the draw.  DEFAULT 0n -- the doctrine: an
   *                        overdrawn account faces no forced recovery
   *                        on-chain; it just cannot extend more credit,
   *                        and the Jubilee fund unwinds the excess over
   *                        time.  10000n restores the old full buy-back.
   * @param opts.aggrBp     optimal-control aggressiveness: the basket
   *                        DISCOUNT (bvib - 1, in bp) this debtor will
   *                        still deploy credit into, because the interest
   *                        drain outweighs it.  0n (default) = the
   *                        conservative deploy-only-at-premium policy;
   *                        550n tolerates ~1 year of 5.5% interest.
   */
  constructor({ house, mortgageBp, premiumBp, payment, account,
                aggrBp = 0n, recoverBp = 0n }) {
    Object.assign(this, { house, mortgageBp, premiumBp, payment, account,
                          aggrBp, recoverBp });
    this.jubileeRelief = 0n;  // accrued lien dissolution (2%/yr on drawn)
    this.mortgageOwing = house;
    this.hypoOwing = house;   // the counterfactual: the mortgage untouched
    this.usdc = 0n;           // banked payments awaiting a discount
    this.hypoBanked = 0n;
    this.ledger = [];         // {day, drawn, mortgageOwing, banked,
                              //  hypoOwing, buckUsd} per month
  }

  async setup(world) {
    // Pledge once: BuckCredit headroom only.  Draws happen on signal.
    await world.pledge(this.account, this.house, { tag: "debtor:pledge" });
  }

  async act(world, day, tick) {
    if (tick !== 0 || day % MONTH !== 0) return;
    const s = world.session, me = world.holderAddress(this.account);
    let buckUsd = this.house * this.premiumBp / BP / 12n;   // insurer's bill
    let budget = this.payment;

    // Service the mortgage first; whatever it no longer eats, bank.
    if (this.mortgageOwing > 0n) {
      const interest = this.mortgageOwing * this.mortgageBp / BP / 12n;
      const pay = min(budget, this.mortgageOwing + interest);
      this.mortgageOwing += interest - pay;
      budget -= pay;
    }
    this.usdc += budget;

    const drawn = -(await s.call(world.buck, "signedBalanceOf", [me]));
    const limit = await s.call(world.buck, "creditLimit", [me]);
    const bvib = await s.call(world.basket, "basketValueInBuck");
    let pos = drawn;               // ledger-accurate position after the legs

    // Jubilee: the fund (2%/yr of supply, dedicated to lien redemption)
    // melts the outstanding obligation; accrue our pro-rata relief.
    const JUB_BP = 200n;
    if (drawn > 0n) {
      this.jubileeRelief = min(drawn,
        this.jubileeRelief + drawn * JUB_BP / BP / 12n);
    }

    if (drawn > limit && this.recoverBp > 0n) {
      // VOLUNTARY overdraw recovery (doctrine: nothing on-chain forces
      // this -- an overdrawn account simply cannot extend more credit;
      // recoverBp scales how hard this debtor chooses to normalize).
      const need = await world.usdcForBuck(
        (drawn - limit) * this.recoverBp / BP);
      const spend = min(this.usdc, need);
      const got = await world.buyBuck(spend, this.account,
                                      { tag: `debtor:forced:d${day}` });
      this.usdc -= spend;
      buckUsd += spend - got;
      pos -= got;
    } else if (bvib < E18 + (this.aggrBp * E18) / BP - BAND
               && this.mortgageOwing > 0n && drawn < limit) {
      // DEFLATION (or a tolerable discount, when aggrBp > 0): sell BUCK
      // and retire expensive mortgage principal.  The optimal-control
      // insight: paying the basket discount now can still win when the
      // interest saved on the retired principal exceeds it.  Tranche-
      // capped (a year of payments) so one lumpy agent cannot slam the
      // floating pool in a single act.
      const draw = min(min(limit - drawn, this.mortgageOwing),
                       this.payment * 12n);
      const got = await world.sellBuck(draw, this.account,
                                       { tag: `debtor:draw:d${day}` });
      this.mortgageOwing -= min(got, this.mortgageOwing);
      buckUsd += draw - got;            // NEGATIVE when sold above par
      pos += draw;
    } else if (bvib > E18 + BAND && drawn > 0n && this.usdc >= 10n ** 6n) {
      // INFLATION: buy discounted BUCK; retire the draw toward zero.
      const spend = min(this.usdc, drawn);
      const got = await world.buyBuck(spend, this.account,
                                      { tag: `debtor:paydown:d${day}` });
      this.usdc -= spend;
      buckUsd += spend - got;           // NEGATIVE when bought below par
      pos -= got;
    }

    this.ledger.push({ day, drawn: pos, buckUsd,
                       jubileeRelief: this.jubileeRelief,
                       mortgageOwing: this.mortgageOwing,
                       banked: this.usdc,
                       hypoOwing: this.#hypoMonth() });
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
