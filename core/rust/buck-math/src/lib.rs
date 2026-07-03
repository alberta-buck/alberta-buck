//! Alberta Buck integer monetary arithmetic.
//!
//! The Solidity contracts are the specification: every function here must
//! be bit-identical to the deployed-code paths, proven by forge-generated
//! golden vectors (`test/vectors/math-*.json`) asserted equally by the
//! Rust, Python, and JS suites (alberta-buck-platform.org, Layer 2).
//!
//! Phase 2 scope (to land here): BUCK_CREDIT declining-balance
//! depreciation (discrete whole-year compounding + linear partial year,
//! 100-year cap), demurrage / buckSeconds carrying apportionment,
//! `fundingFactor()`, the rescaled-ppm PID arithmetic, and fees.
//!
//! Rules of the crate: integer math only -- no floats, ever; `no_std`
//! compatible (Holochain zomes consume this crate); floor division
//! everywhere, matching EVM semantics.

#![no_std]

/// Basis points denominator: 10_000 bp = 1.0.
pub const BP: u128 = 10_000;

/// Parts-per-million denominator: 1_000_000 ppm = 1.0.
pub const PPM: u128 = 1_000_000;

/// Floor of `a * b / den`, the EVM's ubiquitous scaling idiom.
///
/// Widens through `u256`-free territory by requiring the product to fit
/// in u128 -- sufficient for bp/ppm scaling of token amounts up to
/// ~3.4e26 wei.  The alloy-U256 variant arrives with the Phase 2 kernels.
///
/// # Panics
/// On overflow of `a * b` or on `den == 0`, exactly as Solidity 0.8
/// checked arithmetic reverts.
pub fn mul_div(a: u128, b: u128, den: u128) -> u128 {
    a.checked_mul(b).expect("mul_div overflow") / den
}

/// Apply a basis-point rate: floor(amount * rate_bp / 10_000).
pub fn apply_bp(amount: u128, rate_bp: u128) -> u128 {
    mul_div(amount, rate_bp, BP)
}

/// Apply a ppm rate: floor(amount * rate_ppm / 1_000_000).
pub fn apply_ppm(amount: u128, rate_ppm: u128) -> u128 {
    mul_div(amount, rate_ppm, PPM)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floor_semantics() {
        // 1 wei at 1 bp floors to zero -- fee math must never round up.
        assert_eq!(apply_bp(1, 1), 0);
        assert_eq!(apply_bp(10_000, 1), 1);
        assert_eq!(apply_ppm(999_999, 1), 0);
        assert_eq!(apply_ppm(1_000_000, 1), 1);
    }

    #[test]
    fn identity_rates() {
        assert_eq!(apply_bp(123_456_789, BP), 123_456_789);
        assert_eq!(apply_ppm(123_456_789, PPM), 123_456_789);
    }

    #[test]
    #[should_panic(expected = "mul_div overflow")]
    fn overflow_panics_like_solidity_reverts() {
        mul_div(u128::MAX, 2, 1);
    }
}
