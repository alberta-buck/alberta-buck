//! Alberta Buck integer monetary arithmetic.
//!
//! The Solidity contracts are the specification: every function here is
//! bit-identical to the contract primitives, checked by shared vectors in
//! test/vectors. Canonical net-math vectors use an independent Python-integer
//! oracle checked against Solidity; legacy vectors are forge-generated.
//! Rust, Python, Node and browser WASM consume the same canonical cases.
//!
//! Modules mirror the contracts:
//!   * [`depreciation`] -- BuckCredit `_depreciate` / `currentValue`
//!   * [`net_demurrage`] -- canonical BuckCash compounded cash / NFT relief
//!   * [`uint256`]      -- full uint256 values and 512-bit multiply/divide
//!   * [`demurrage`]    -- legacy BuckLinear `_feeOwing` / `_carryingTransfer`
//!   * [`pid`]          -- BuckKControllerDirect rescaled-ppm PID +
//!     fundingFactor + bumpless governance algebra
//!   * [`wide`]         -- full-width mulDiv (OZ Math.mulDiv semantics)
//!
//! Rules of the crate: integer math only -- no floats, ever; `no_std`
//! and dependency-free (Holochain zomes consume this crate), with explicit
//! floor/ceiling division. Canonical arithmetic returns domain or
//! overflow errors where Solidity reverts; legacy helpers panic. Release
//! overflow checks stay enabled as well.

#![no_std]

pub mod carry;
pub mod demurrage;
pub mod net_demurrage;
pub mod uint256;
pub mod depreciation;
pub mod pid;
pub mod wide;

pub use carry::{carry_to_net, carry_to_net_scaled, carry_to_nominal, quote_at_tick, sqrt_ratio_at_tick};
pub use demurrage::{carrying_transfer, fee_owing, CarryingResult};
pub use net_demurrage::{net_decay, net_fund_after, net_target_relief};
pub use uint256::{mul_div_256, NetMathError, U256};
pub use depreciation::{current_value, depreciate, DepType};
pub use pid::DirectPid;
pub use wide::{mul_div_wide, mul_wide};

// ── Shared constants (values from Buck.sol / BuckCredit.sol / BuckTypes.sol) ──

/// Basis points denominator: 10_000 bp = 1.0.
pub const BP: u128 = 10_000;

/// Parts-per-million denominator: 1_000_000 ppm = 1.0.
pub const PPM: u128 = 1_000_000;

/// 365 days + 6 hours, the year both Buck and BuckCredit use.
pub const SECONDS_PER_YEAR: u128 = 31_557_600;

/// DECLINING_BALANCE compounding cap (BuckCredit.MAX_DEP_YEARS).
pub const MAX_DEP_YEARS: u128 = 100;

/// Legacy linear demurrage scale (BuckLinear.SCALE); canonical NET_SCALE is 1e36.
pub const SCALE: u128 = 1_000_000_000_000_000_000_000_000_000; // 1e27

/// Legacy 0.02/year in SCALE (BuckLinear.BASE_RATE_PER_YEAR).
pub const BASE_RATE_PER_YEAR: u128 = 20_000_000_000_000_000_000_000_000; // 2e25

/// Legacy per-second demurrage rate: the floored division BuckLinear
/// performs at compile time.
pub const BASE_RATE_PER_SEC: u128 = BASE_RATE_PER_YEAR / SECONDS_PER_YEAR;

/// buckSeconds cap (BuckTypes.MAX_BS = uint120 max).
pub const MAX_BS: u128 = (1 << 120) - 1;

/// Signed balance cap (BuckTypes toBuckQtySigned int80 range).
pub const MAX_BALANCE_SIGNED: i128 = (1 << 79) - 1;

/// 1.0 in 18-decimal fixed point (controller UNIT).
pub const UNIT: i128 = 1_000_000_000_000_000_000;

/// ppm -> 1e18 rescale factor (BuckKControllerDirect.TO18).
pub const TO18: i128 = 1_000_000_000_000;

// ── Narrow scaling helpers (Phase 0 seed; still handy) ──────────────────

/// Floor of `a * b / den` where the product fits u128.
///
/// # Panics
/// On overflow of `a * b` or `den == 0`, as Solidity 0.8 reverts.  Use
/// [`mul_div_wide`] when the product can exceed 128 bits.
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

    #[test]
    fn constants_match_the_contracts() {
        assert_eq!(SECONDS_PER_YEAR, 365 * 86_400 + 6 * 3_600);
        assert_eq!(BASE_RATE_PER_SEC, 633_761_756_280_579_004);
        assert_eq!(MAX_BS, 1_329_227_995_784_915_872_903_807_060_280_344_575);
    }
}
