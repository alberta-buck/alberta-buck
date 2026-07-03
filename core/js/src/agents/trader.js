// RoundTripTrader: the walking-skeleton "arb-shaped" agent.  Each day it
// sells a fixed TOKEN amount into the pool and buys back with the quote
// proceeds -- two real V3 swaps through SimLP, both journaled.  On day 0
// it also demonstrates a DECLARED failure: a swap whose price limit is on
// the wrong side, sent with expect:"revert" (Uniswap's SPL check), which
// the journal records as matched rather than an error.
//
// Swap mechanics (SimLP pays the pool from its own balance): fund SimLP
// with the input token, then SimLP.swap(pool, recipient=self, ...) sends
// the output to the trader.

import { MIN_SQRT_RATIO, MAX_SQRT_RATIO } from "../v3.js";

export class RoundTripTrader {
  /**
   * @param opts.simlp  SimLP handle    @param opts.pool  pool handle
   * @param opts.token  TOKEN handle (ERC20 abi) -- the trader's base asset
   * @param opts.quote  quote handle (ERC20 abi)
   * @param opts.amount TOKEN base units to round-trip each day
   * @param opts.account viem Account for this agent (defaults to session's)
   */
  constructor({ simlp, pool, token, quote, amount, account = undefined }) {
    Object.assign(this, { simlp, pool, token, quote, amount, account });
  }

  async setup(world) {
    this.tokenIs0 =
      this.token.address.toLowerCase() < this.quote.address.toLowerCase();
    this.me = (this.account ?? world.session.account).address;
    this.opts = { account: this.account };
  }

  #swapArgs(zeroForOne, amountIn) {
    // exact-in (positive amount), price limit wide open on the swap side
    const limit = zeroForOne ? MIN_SQRT_RATIO + 1n : MAX_SQRT_RATIO - 1n;
    const [t0, t1] = this.tokenIs0
      ? [this.token.address, this.quote.address]
      : [this.quote.address, this.token.address];
    return [this.pool.address, this.me, zeroForOne, amountIn, limit, t0, t1];
  }

  async act(world, day, tick) {
    if (tick !== 0) return;
    const s = world.session;

    if (day === 0) {
      // Declared failure: zeroForOne with a limit ABOVE spot always
      // violates Uniswap's sqrt-price-limit require ("SPL").
      const bad = this.#swapArgs(true, this.amount);
      bad[4] = MAX_SQRT_RATIO - 1n;
      await s.send(this.simlp, "swap", bad,
        { ...this.opts, expect: "revert", tag: `trader:spl-demo:d${day}` });
    }

    // Sell TOKEN -> quote: fund SimLP, swap with self as recipient.
    const quoteBefore = await s.call(this.quote, "balanceOf", [this.me]);
    await s.send(this.token, "transfer", [this.simlp.address, this.amount],
                 { ...this.opts, tag: `trader:fund:d${day}` });
    await s.send(this.simlp, "swap", this.#swapArgs(this.tokenIs0, this.amount),
                 { ...this.opts, tag: `trader:sell:d${day}` });
    const quoteAfter = await s.call(this.quote, "balanceOf", [this.me]);
    const proceeds = quoteAfter - quoteBefore;

    // Buy back TOKEN with the proceeds.
    await s.send(this.quote, "transfer", [this.simlp.address, proceeds],
                 { ...this.opts, tag: `trader:refund:d${day}` });
    await s.send(this.simlp, "swap", this.#swapArgs(!this.tokenIs0, proceeds),
                 { ...this.opts, tag: `trader:buyback:d${day}` });
  }
}
