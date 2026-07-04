// PROTOTYPE -- a structural exemplar, not yet wired to a world.
//
// The flagship example story for the JS platform (alberta-buck-platform.org,
// "The JS agent doctrine").  One screen, plain object, every send tagged,
// and the agent is its own accountant: this.ledger holds BOTH columns of
// the mortgage-vs-BuckCredit comparison chart.
//
// Awaits the equilibrium-world builder (Stage 6) for these helpers:
//   world.pledge(account, face, {tag})     createCredit + activate headroom
//   world.sellBuck(buckIn, account, {tag}) -> USDC received (draws the credit)
//   world.buyBuck(usdcIn, account, {tag})  -> BUCK received (retires the draw)
//   world.usdcForBuck(buckOut)             -> USDC needed at current spot
// plus world.session / world.buck / world.basket contract handles.
//
// MortgageRetiree: retires a 25-year bank mortgage with BuckCredit, then
// pays the position down opportunistically over the years.
//
// Day 0: pledge the insured house -> BuckCredit NFT -> activate the face
// as credit headroom -> DRAW by selling BUCK for USDC -> pay out the
// bank.  From then on the signed BUCK balance is NEGATIVE: an outstanding
// claim on the retiree's own pledged asset.  It carries no interest and
// no demurrage (demurrage applies to positive balances) -- only the
// insurance premium.  That asymmetry vs. amortizing interest is what the
// chart demonstrates.
//
// Monthly: income arrives (USDC).  When BUCK trades BELOW basket value
// (basketValueInBuck > 1e18 -- the same observable the controller
// defends), buy discounted BUCK: the balance rises toward zero and the
// obligation retires cheaply.  When BUCK is above value, wait; holding
// USDC income costs nothing.
//
// The risk stays real: creditLimit = collateralValue * buckK moves with
// the controller.  If K tightens past the draw, the retiree MUST buy
// BUCK at whatever the market asks -- booked as a forced cost, exactly
// the number a credit-union analyst needs to see.

const BP = 10_000n;
const MONTH = 30;
const min = (a, b) => (a < b ? a : b);

export class MortgageRetiree {
  /**
   * @param opts.house      insured-asset face (BUCK/USDC are both 6-dec)
   * @param opts.mortgageBp abandoned mortgage's annual rate (e.g. 550n)
   * @param opts.premiumBp  annual insurance premium on the face (e.g. 50n)
   * @param opts.income     monthly free cash flow, USDC base units
   * @param opts.account    funded account with a REAL registered identity
   */
  constructor({ house, mortgageBp, premiumBp, income, account }) {
    Object.assign(this, { house, mortgageBp, premiumBp, income, account });
    this.hypoOwing = house;   // the counterfactual loan, amortizing beside us
    this.usdc = 0n;           // income accumulated, awaiting a discount
    this.ledger = [];         // {day, drawn, mortgageUsd, buckUsd} per month
  }

  async setup(world) {
    // Pledge once, activate the full face once (headroom only -- BUCK
    // enters circulation as we draw it on the next line).  The premium
    // is booked in the ledger as the insurer's monthly bill.
    await world.pledge(this.account, this.house, { tag: "retiree:pledge" });
    // Draw the whole face and retire the bank loan with the proceeds.
    await world.sellBuck(this.house, this.account, { tag: "retiree:draw" });
  }

  async act(world, day, tick) {
    if (tick !== 0 || day % MONTH !== 0) return;
    const s = world.session, me = this.account.address;
    this.usdc += this.income;
    let buckCost = this.house * this.premiumBp / BP / 12n;  // insurer's bill

    const drawn = -(await s.call(world.buck, "signedBalanceOf", [me]));
    const limit = await s.call(world.buck, "creditLimit", [me]);

    // 1. Forced deleverage: the controller tightened K past our draw.
    if (drawn > limit) {
      const spend = min(this.usdc, world.usdcForBuck(drawn - limit));
      const got = await world.buyBuck(spend, this.account,
                                      { tag: `retiree:forced:d${day}` });
      this.usdc -= spend;
      buckCost += spend - got;          // premium paid above parity
    }

    // 2. Opportunistic paydown: buy the obligation back at a discount.
    const bvib = await s.call(world.basket, "basketValueInBuck");
    if (drawn > 0n && bvib > 10n ** 18n && this.usdc >= 10n ** 6n) {
      const spend = min(this.usdc, min(drawn, this.income * 3n));
      const got = await world.buyBuck(spend, this.account,
                                      { tag: `retiree:paydown:d${day}` });
      this.usdc -= spend;
      buckCost += spend - got;          // NEGATIVE when bought below par
    }

    // 3. Both chart columns, from one ledger row.
    this.ledger.push({ day, drawn,
                       mortgageUsd: this.#mortgageMonth(),
                       buckUsd: buckCost });
  }

  #mortgageMonth() {
    // What the abandoned mortgage would have cost this month: interest
    // at the fixed rate, paid down by the same monthly income.
    if (this.hypoOwing === 0n) return 0n;
    const interest = this.hypoOwing * this.mortgageBp / BP / 12n;
    const payment = min(this.income, this.hypoOwing + interest);
    this.hypoOwing += interest - payment;
    return interest;
  }
}
