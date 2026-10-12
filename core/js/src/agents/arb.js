// ParityArb: keeps the pool TRIANGLE consistent -- TOKEN/BUCK vs the
// price implied by TOKEN/USDC x BUCK/USDC.  When the basket pool drifts
// from the implied price by more than thresholdBp, it routes the
// profitable triangle through the REAL Universal Router (both legs
// start and end in USDC, so the arb needs no identity: BUCK only
// passes through the bound router mid-hop), sized small and iterated
// until the gap closes or maxIters runs out.
//
// On the basket machine the basket itself trades its pools, every turn of
// its wheel and in size, so a fixed small triangle cannot keep up.  There
// each triangle is sized to the gap (closeBp of what would close it), and
// the WheelKeeper runs a second pass after the wheel has turned.
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
   * @param opts.closeBp     size each triangle to this share (bp) of what
   *                         would close the gap, never under `size`; 0 keeps
   *                         `size` fixed.  Default: 8000 where the basket has
   *                         a wheel (the machine), 0 elsewhere
   * @param opts.maxSize     the most one triangle may be, USDC base units
   */
  constructor({ size = 2_000n * 10n ** 6n, thresholdBp = 100n,
                maxIters = 3, closeBp = null,
                maxSize = 1_000_000n * 10n ** 6n } = {}) {
    Object.assign(this, { size, thresholdBp, maxIters, closeBp, maxSize });
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

        // A triangle moves all three pools toward agreement.  In a
        // full-range pool the price moves by twice the share of the reserve
        // traded, so x closes 2x(1/Rtb + 1/Rtu + 1/Rub) of the gap, the
        // reserves in USDC: size it for closeBp of the gap.
        let size = this.size;
        const closeBp = this.closeBp ?? (world.wheel ? 8_000n : 0n);
        if (closeBp > 0n) {
          const [rtb, rtu, rub] = await Promise.all([
            world.session.call(buck, "balanceOf", [t.poolBuck.address]),
            world.session.call(usdc, "balanceOf", [t.poolUsdc.address]),
            world.session.call(usdc, "balanceOf", [world.poolUB.address])]);
          const a = rtb * pUB / 1_000_000n;            // the BUCK side, in USDC
          const sum = a * rtu + rtu * rub + rub * a;
          const gap = div < 0n ? -div : div;
          const want = sum > 0n
            ? ((a * rtu * rub / sum) * gap / 20_000n) * closeBp / 10_000n : 0n;
          size = want > this.maxSize ? this.maxSize : want > size ? want : size;
        }

        // TOKEN rich in BUCK: buy it direct, sell it via the BUCK legs
        // (and vice versa).  Both directions push pTB toward implied.
        const inPath = div > 0n
          ? [usdc.address, fees.usdc, t.erc20.address]
          : [usdc.address, fees.ub, buck.address, fees.buck, t.erc20.address];
        const outPath = div > 0n
          ? [t.erc20.address, fees.buck, buck.address, fees.ub, usdc.address]
          : [t.erc20.address, fees.usdc, usdc.address];
        const got = await world.route(inPath, size, this.me,
          { tag: `arb:${t.sym}:in:d${day}` });
        await world.route(outPath, got, this.me,
          { tag: `arb:${t.sym}:out:d${day}` });
        this.triangles++;
      }
    }
  }
}
