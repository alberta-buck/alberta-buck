//! Carried BUCK, and a pool's price at a tick, exactly as the contracts compute them.
//!
//! Buck's ERC-20 view is nominal. A Carrying account (a pool, the router, the basket) carries its
//! accrued demurrage instead of paying it, so its nominal BUCK is worth net / gross of full-value
//! BUCK, and a transfer out of it delivers that share (BuckCash.move). These are
//! src/lib/CarryRatio.sol's conversions -- convert with the ratio of the account the BUCK leaves;
//! a price in BUCK per TOKEN converts like an amount; an empty account (gross 0) carries nothing --
//! and Uniswap V3's quote at a tick (src/lib/UniswapV3OracleLib.sol: TickMath.getSqrtRatioAtTick,
//! OracleLibrary.getQuoteAtTick), integer for integer.

use crate::uint256::{mul_div_256, NetMathError, U256};
use crate::wide::mul_wide;

/// The 1e36 scale of Buck's scaled balances.
pub const Q: u128 = 1_000_000_000_000_000_000_000_000_000_000_000_000;
pub const MAX_TICK: i32 = 887_272;

/// Net value of `nominal` BUCK (an amount, or a price in BUCK) leaving an account with this
/// (gross, net), rounded down (CarryRatio.toNet).
pub fn carry_to_net(gross: U256, net: U256, nominal: U256) -> Result<U256, NetMathError> {
    if gross == U256::ZERO || gross == net {
        return Ok(nominal);
    }
    mul_div_256(nominal, net, gross, false)
}

/// What BuckCash.move delivers when the account sends `nominal` raw BUCK: net raw BUCK x 1e36,
/// rounded down (CarryRatio.toNetScaled).
pub fn carry_to_net_scaled(gross: U256, net: U256, nominal: U256) -> Result<U256, NetMathError> {
    let scaled = mul_div_256(nominal, U256::from_u128(Q), U256::ONE, false)?;
    if gross == U256::ZERO {
        return Ok(scaled);
    }
    mul_div_256(scaled, net, gross, false)
}

/// Nominal raw BUCK the account must send to deliver at least `value` net, rounded up
/// (CarryRatio.toNominal); refused when the account has no net value.
pub fn carry_to_nominal(gross: U256, net: U256, value: U256) -> Result<U256, NetMathError> {
    if gross == U256::ZERO || gross == net {
        return Ok(value);
    }
    if net == U256::ZERO {
        return Err(NetMathError::NoNetValue);
    }
    mul_div_256(value, gross, net, true)
}

/// TickMath.getSqrtRatioAtTick: sqrt(1.0001^tick) x 2^96, rounded up.
pub fn sqrt_ratio_at_tick(tick: i32) -> Result<U256, NetMathError> {
    let abs = tick.unsigned_abs();
    if abs > MAX_TICK as u32 {
        return Err(NetMathError::TickOutOfRange);
    }
    const STEPS: [(u32, u128); 19] = [
        (0x2, 0xfff97272373d413259a46990580e213a), (0x4, 0xfff2e50f5f656932ef12357cf3c7fdcc),
        (0x8, 0xffe5caca7e10e4e61c3624eaa0941cd0), (0x10, 0xffcb9843d60f6159c9db58835c926644),
        (0x20, 0xff973b41fa98c081472e6896dfb254c0), (0x40, 0xff2ea16466c96a3843ec78b326b52861),
        (0x80, 0xfe5dee046a99a2a811c461f1969c3053), (0x100, 0xfcbe86c7900a88aedcffc83b479aa3a4),
        (0x200, 0xf987a7253ac413176f2b074cf7815e54), (0x400, 0xf3392b0822b70005940c7a398e4b70f3),
        (0x800, 0xe7159475a2c29b7443b29c7fa6e889d9), (0x1000, 0xd097f3bdfd2022b8845ad8f792aa5825),
        (0x2000, 0xa9f746462d870fdf8a65dc1f90e061e5), (0x4000, 0x70d869a156d2a1b890bb3df62baf32f7),
        (0x8000, 0x31be135f97d08fd981231505542fcfa6), (0x10000, 0x9aa508b5b7a84e1c677de54f3e99bc9),
        (0x20000, 0x5d6af8dedb81196699c329225ee604), (0x40000, 0x2216e584f5fa1ea926041bedfe98),
        (0x80000, 0x48a170391f7dc42444e8fa2),
    ];
    // The ratio starts at 2^128 (one past u128) unless the lowest bit is set; a product with
    // 2^128, shifted down 128 bits, is the factor itself. Every later ratio fits 128 bits.
    let mut ratio: Option<u128> = (abs & 1 != 0).then_some(0xfffcb933bd6fad37aa2d162d1a594001);
    for (bit, factor) in STEPS {
        if abs & bit != 0 {
            ratio = Some(match ratio {
                Some(r) => mul_wide(r, factor).0,
                None => factor,
            });
        }
    }
    let mut ratio = match ratio {
        Some(r) => U256::from_u128(r),
        None => U256::pow2(128),
    };
    if tick > 0 {
        ratio = mul_div_256(U256::MAX, U256::ONE, ratio, false)?;
    }
    // (ratio >> 32) + (ratio % 2^32 == 0 ? 0 : 1): a ceiling division by 2^32.
    mul_div_256(ratio, U256::ONE, U256::pow2(32), true)
}

/// OracleLibrary.getQuoteAtTick: `base_amount` of the base token in the quote token at `tick`
/// (token1 per token0 is 1.0001^tick); `base_is_token0` when the base token's address is the lower.
pub fn quote_at_tick(tick: i32, base_amount: u128, base_is_token0: bool) -> Result<U256, NetMathError> {
    let sqrt = sqrt_ratio_at_tick(tick)?;
    let base = U256::from_u128(base_amount);
    let (ratio, one) = match sqrt.as_u128() {
        Some(s) => {
            let (hi, lo) = mul_wide(s, s);
            (U256::from_halves(hi, lo), U256::pow2(192))
        }
        None => (mul_div_256(sqrt, sqrt, U256::pow2(64), false)?, U256::pow2(128)),
    };
    if base_is_token0 {
        mul_div_256(ratio, base, one, false)
    } else {
        mul_div_256(one, base, ratio, false)
    }
}
