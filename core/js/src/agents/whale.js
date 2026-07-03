// PinWhale: snaps a TOKEN/quote V3 pool to a reference price path --
// the JS peer of the Python sim's MarketMakerWhale.snap().
//
// The V3 trick (identical to agents.py): swap an effectively unlimited
// amount with sqrtPriceLimitX96 = the target -- the pool executes exactly
// enough volume to land on the limit.  Paid from the SimLP helper's own
// token custody, so the whale only needs SimLP to be stocked (the Python
// deploy leaves it holding deep TOKEN+quote reserves).

import { HUGE, sqrtPriceX96 } from "../v3.js";

export class PinWhale {
  /**
   * @param opts.simlp   SimLP contract handle
   * @param opts.pool    UniswapV3Pool handle (TOKEN/quote)
   * @param opts.token   TOKEN address   @param opts.tokenDec its decimals
   * @param opts.quote   quote address (e.g. USDC)
   * @param opts.refPrice (day) => quote base units per whole TOKEN
   */
  constructor({ simlp, pool, token, tokenDec, quote, refPrice }) {
    Object.assign(this, { simlp, pool, token, tokenDec, quote, refPrice });
  }

  async setup(world) {
    const [t0, t1] = await Promise.all([
      world.session.call(this.pool, "token0"),
      world.session.call(this.pool, "token1"),
    ]);
    this.t0 = t0;
    this.t1 = t1;
  }

  async act(world, day, tick) {
    if (tick !== 0) return;                       // one snap per day
    const slot0 = await world.session.call(this.pool, "slot0");
    const cur = slot0[0];                          // sqrtPriceX96
    const targ = sqrtPriceX96(this.token, 10n ** BigInt(this.tokenDec),
                              this.quote, this.refPrice(day));
    if (cur === targ) return;
    await world.session.send(this.simlp, "swap",
      [this.pool.address, this.simlp.address, targ < cur, HUGE, targ,
       this.t0, this.t1],
      { tag: `whale:snap:d${day}` });
  }
}
