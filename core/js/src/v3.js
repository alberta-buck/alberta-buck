// Uniswap V3 price/tick math, BigInt throughout -- mirrors
// alberta_buck/sim/router.py (sqrt_price_x96, full_range_ticks) exactly.

export const MIN_SQRT_RATIO = 4295128739n;
export const MAX_SQRT_RATIO =
  1461446703485210103287273052203988822378723970342n;

/** int256 "swap everything until the price limit" amount (whale snaps). */
export const HUGE = (1n << 127n) - 1n;

export const Q96 = 1n << 96n;

/** Integer square root (floor), Newton's method on BigInt. */
export function isqrt(n) {
  if (n < 0n) throw new Error("isqrt of negative");
  if (n < 2n) return n;
  let x = 1n << (BigInt(n.toString(2).length + 1) >> 1n);
  for (;;) {
    const y = (x + n / x) >> 1n;
    if (y >= x) return x;
    x = y;
  }
}

/** sqrtPriceX96 so 1 `baseAmt` of base costs `quoteAmt` of quote
 *  (token0/token1 by address order, exactly as router.py). */
export function sqrtPriceX96(base, baseAmt, quote, quoteAmt) {
  const baseIs0 = base.toLowerCase() < quote.toLowerCase();
  const a0 = baseIs0 ? baseAmt : quoteAmt;
  const a1 = baseIs0 ? quoteAmt : baseAmt;
  return isqrt((a1 << 192n) / a0);
}

/** Full-range tick bounds for a spacing; truncates toward zero to stay
 *  inside [-887272, 887272], matching Solidity (MIN_TICK/spacing)*spacing. */
export function fullRangeTicks(spacing) {
  const hi = Math.trunc(887272 / spacing) * spacing;
  return [-hi, hi];
}

/** Spot price of one whole token (10^dec base units) in quote base units,
 *  from a pool's sqrtPriceX96.  The inverse of sqrtPriceX96(). */
export function spotFromSqrtPriceX96(sqrtX96, token, tokenDec, quote) {
  const tokenIs0 = token.toLowerCase() < quote.toLowerCase();
  const unit = 10n ** BigInt(tokenDec);
  const num = sqrtX96 * sqrtX96;
  return tokenIs0 ? (num * unit) >> 192n : (unit << 192n) / num;
}
