// PROTOTYPE -- a structural exemplar, not yet wired to a world.
//
// This is the JS agent doctrine made concrete (alberta-buck-platform.org,
// "The JS agent doctrine"): a plain object with setup(world) and
// act(world, day, tick); every chain mutation through session.send with a
// tag; analytics in a plain this.ledger array the chart panels read.  It
// must fit on one screen and read cold to a senior integration developer
// at a credit union -- the agents ARE the SDK documentation.
//
// Awaits the equilibrium-world builder (Stage 6) for these helpers, which
// carry ALL plumbing (custody, identity binding, router encoding):
//   world.basketDeposit(token, amount, account, {tag}) -> {receiptId, buckOut}
//   world.basketRedeem(receiptId, account, {tag})      -> USD received (6-dec)
//   world.refUsd(token, day, amount)                   -> USD value at day refs
//   world.receiptValueUsd(receiptId, day)              -> instantaneous value
//
// BasketInvestor: puts real backing into the BUCK system and books the
// round trip.  depositToken() takes the TOKEN, mints BUCK against it,
// and LPs the pair into the TOKEN/BUCK pool -- the depositor holds only
// the receipt NFT (the claim the demo shows); redeem() later pays out
// pro-rata.  ROI accounting follows the Python DM agents: USD committed
// at deposit-day reference prices vs USD received at redemption, with
// dollar-days so returns annualize.

export class BasketInvestor {
  /**
   * @param opts.token    basket-constituent ERC20 handle (e.g. NRGC)
   * @param opts.amount   TOKEN base units per deposit
   * @param opts.holdDays days from deposit to redemption
   * @param opts.account  this agent's funded account
   */
  constructor({ token, amount, holdDays = 180, account }) {
    Object.assign(this, { token, amount, holdDays, account });
    this.position = null;   // {receiptId, day, costUsd}
    this.ledger = [];       // {day, event, usd} -- the ROI chart reads this
    this.profitUsd = 0n;
    this.dollarDays = 0n;   // annualized ROI = profitUsd * 365n / dollarDays
  }

  async setup(world) {}

  async act(world, day, tick) {
    if (tick !== 0) return;

    if (!this.position) {
      // TOKEN in -> receipt out (the basket mints BUCK and LPs the
      // pair itself).  The basket is a public identity-bound contract,
      // so no bilateral handshake is needed.
      const { receiptId } = await world.basketDeposit(
        this.token, this.amount, this.account,
        { tag: `invest:deposit:d${day}` });
      const costUsd = world.refUsd(this.token, day, this.amount);
      this.position = { receiptId, day, costUsd };
      this.ledger.push({ day, event: "deposit", usd: -costUsd });
      return;
    }

    // Mark the receipt to market daily -- the "instantaneous value" line.
    const mark = await world.receiptValueUsd(this.position.receiptId, day);
    this.ledger.push({ day, event: "mark", usd: mark });

    if (day - this.position.day >= this.holdDays) {
      const gotUsd = await world.basketRedeem(
        this.position.receiptId, this.account,
        { tag: `invest:redeem:d${day}` });
      this.profitUsd += gotUsd - this.position.costUsd;
      this.dollarDays += this.position.costUsd
                       * BigInt(day - this.position.day);
      this.ledger.push({ day, event: "redeem", usd: gotUsd });
      this.position = null;                  // free to re-enter
    }
  }
}
