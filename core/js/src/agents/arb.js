// ParityArb: keeps the pool TRIANGLE consistent -- TOKEN/BUCK vs the
// price implied by TOKEN/USDC x BUCK/USDC.  When the basket pool drifts
// from the implied price by more than thresholdBp, it routes the
// profitable triangle through the REAL Universal Router (both legs
// start and end in USDC, so the arb needs no identity: BUCK only
// passes through the bound router mid-hop), sized small and iterated
// until the gap closes or maxIters runs out.
//
// This is the agent that makes basketValueInBuck MEAN something: the
// controller reads the TOKEN/BUCK pools, the whale pins TOKEN/USDC to
// the reference feeds, and the arb ties the two together -- so BUCK
// supply/demand pressure in the floating BUCK/USDC pool propagates
// into the observable the PID defends.

export class ParityArb {
  /**
   * @param opts.size        USDC base units per triangle leg
   * @param opts.thresholdBp act when |divergence| exceeds this
   * @param opts.maxIters    triangles per token per tick
   */
  constructor({ size = 2_000n * 10n ** 6n, thresholdBp = 100n,
                maxIters = 3 } = {}) {
    Object.assign(this, { size, thresholdBp, maxIters });
    this.triangles = 0;          // ops counter, for the curious
  }

  async setup(world) {
    this.me = world.session.account;    // the deployer account arbs
    await world.fiatIn(this.me, 10_000_000n * 10n ** 6n,
                       { tag: "arb:endow" });
  }

  async act(world, day, tick) {
    if (tick !== 0) return;
    const { usdc, buck, fees } = world;
    for (let i = 0; i < world.tokens.length; i++) {
      const t = world.tokens[i];
      for (let iter = 0; iter < this.maxIters; iter++) {
        const [pTU, pUB, pTB] = await Promise.all(
          [world.spotUsd(i), world.spotUB(), world.spotBuck(i)]);
        const implied = (pTU * 1_000_000n) / pUB;
        const div = ((pTB - implied) * 10_000n) / implied;
        if (div > -this.thresholdBp && div < this.thresholdBp) break;

        // TOKEN rich in BUCK: buy it direct, sell it via the BUCK legs
        // (and vice versa).  Both directions push pTB toward implied.
        const inPath = div > 0n
          ? [usdc.address, fees.usdc, t.erc20.address]
          : [usdc.address, fees.ub, buck.address, fees.buck, t.erc20.address];
        const outPath = div > 0n
          ? [t.erc20.address, fees.buck, buck.address, fees.ub, usdc.address]
          : [t.erc20.address, fees.usdc, usdc.address];
        const got = await world.route(inPath, this.size, this.me,
          { tag: `arb:${t.sym}:in:d${day}` });
        await world.route(outPath, got, this.me,
          { tag: `arb:${t.sym}:out:d${day}` });
        this.triangles++;
      }
    }
  }
}
