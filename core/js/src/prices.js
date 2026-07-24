// Seeded synthetic price walk -- deterministic BigInt-only math so any
// platform (or a re-run) reproduces the same path bit-for-bit.  The demo's
// stand-in for the Python sim's CSV Brownian-bridge quotes; the Stage-4
// parity mini-scenario pins its targets in core/vectors/mini-scenario.json
// instead (no cross-language RNG at all).

/** A 64-bit LCG (MMIX constants) over BigInt. */
export function lcg(seed) {
  let s = BigInt(seed) & ((1n << 64n) - 1n);
  return () => {
    s = (s * 6364136223846793005n + 1442695040888963407n) & ((1n << 64n) - 1n);
    return s;
  };
}

/**
 * A multiplicative random walk: each step moves the price by a draw in
 * [-stepBp, +stepBp] basis points.
 *
 * @param opts.seed    walk seed
 * @param opts.start   starting price (quote base units per whole token)
 * @param opts.stepBp  max per-step move in basis points (default 150)
 * @param opts.steps   path length
 * @returns BigInt[] of length `steps` (walk[0] === start)
 */
export function seededWalk({ seed, start, stepBp = 150, steps }) {
  const next = lcg(seed);
  const out = [BigInt(start)];
  const span = BigInt(2 * stepBp + 1);
  while (out.length < steps) {
    const delta = (next() % span) - BigInt(stepBp);   // [-stepBp, +stepBp]
    const prev = out[out.length - 1];
    let p = (prev * (10_000n + delta)) / 10_000n;
    if (p < 1n) p = 1n;
    out.push(p);
  }
  return out;
}
