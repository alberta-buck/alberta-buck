//! Full-width multiply-then-divide, mirroring OpenZeppelin `Math.mulDiv`
//! (floor semantics, revert -- here: panic -- when the result exceeds
//! 128 bits or the denominator is zero).  Needed wherever a Solidity
//! uint256 intermediate exceeds u128: demurrage fees (buckSeconds x
//! rate ~ 8e44) and carrying apportionment (buckSeconds x value).

/// 256-bit product of two u128s as (hi, lo) 128-bit halves.
pub fn mul_wide(a: u128, b: u128) -> (u128, u128) {
    const M: u128 = (1 << 64) - 1;
    let (a1, a0) = (a >> 64, a & M);
    let (b1, b0) = (b >> 64, b & M);
    let ll = a0 * b0;
    let lh = a0 * b1;
    let hl = a1 * b0;
    let hh = a1 * b1;
    let mid = (ll >> 64) + (lh & M) + (hl & M);
    let lo = (ll & M) | ((mid & M) << 64);
    let hi = hh + (lh >> 64) + (hl >> 64) + (mid >> 64);
    (hi, lo)
}

/// floor(a * b / den) with a 256-bit intermediate product.
///
/// # Panics
/// If `den == 0` or the quotient exceeds u128 -- the same conditions
/// under which Solidity's checked math / OZ mulDiv revert.
pub fn mul_div_wide(a: u128, b: u128, den: u128) -> u128 {
    assert!(den != 0, "mul_div: division by zero");
    let (hi, lo) = mul_wide(a, b);
    assert!(hi < den, "mul_div: overflow");
    if hi == 0 {
        return lo / den;
    }
    // Shift-subtract long division of the 256-bit (hi, lo) by den.  The
    // remainder is always < den; when its top bit shifts out, the true
    // (129-bit) value exceeds den, and wrapping_sub yields the correct
    // reduced remainder since 2*rem + bit - den < 2^128.
    let mut rem: u128 = 0;
    let mut q: u128 = 0;
    for i in (0..256).rev() {
        let bit = if i >= 128 { (hi >> (i - 128)) & 1 } else { (lo >> i) & 1 };
        let carry = rem >> 127;
        rem = (rem << 1) | bit;
        if carry == 1 || rem >= den {
            rem = rem.wrapping_sub(den);
            if i < 128 {
                q |= 1 << i;
            }
        }
    }
    q
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn small_products_match_native() {
        assert_eq!(mul_div_wide(6, 7, 2), 21);
        assert_eq!(mul_div_wide(10, 10, 3), 33); // floor
    }

    #[test]
    fn wide_product() {
        // (2^100)^2 / 2^100 == 2^100 -- product needs 200 bits.
        let x = 1u128 << 100;
        assert_eq!(mul_div_wide(x, x, x), x);
        // A demurrage-shaped case: bs (uint120 max) * BASE_RATE_PER_SEC / SCALE.
        let bs = 1_329_227_995_784_915_872_903_807_060_280_344_575u128;
        let rate = 633_761_756_280_579_004u128; // floor(2e25 / 31_557_600)
        let scale = 10u128.pow(27);
        // Cross-checked against Python: bs * rate // scale
        assert_eq!(mul_div_wide(bs, rate, scale),
                   842_413_869_105_962_349_070_037_475u128);
    }

    #[test]
    #[should_panic(expected = "mul_div: overflow")]
    fn overflowing_quotient_panics() {
        mul_div_wide(u128::MAX, u128::MAX, 1);
    }
}
