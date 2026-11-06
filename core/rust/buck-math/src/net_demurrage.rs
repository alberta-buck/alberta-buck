//! Canonical BuckCash / NetDemurrageMath arithmetic. Wallet cash compounds;
//! relief belongs to each NFT's issued principal and accumulates linearly.
//! Amount units are caller-selected: cash normally uses 1e36 per raw BUCK,
//! while target_relief uses raw principal and raw BUCK-seconds.

use crate::uint256::{mul_div_256, NetMathError, U256};

pub const NET_SCALE: u128 = 10u128.pow(36);
pub const SECOND_FACTOR: u128 = 999_999_999_366_238_243_920_247_977_078_958_586;
pub const CHUNK_FACTOR: u128 = 2_061_153_622_438_557_827_965_940_310;
pub const RELIEF_SECONDS: u64 = 50 * 31_557_600;
pub const CHUNK_SECONDS: u64 = 1000 * 31_557_600;
pub const EMPTY_SECONDS: u64 = 9000 * 31_557_600;

fn scaled(a: U256, b: u128) -> Result<U256, NetMathError> {
    mul_div_256(a, U256::from_u128(b), U256::from_u128(NET_SCALE), false)
}

/// Post: result <= amount. Even a maximum uint256 elapsed is bounded work:
/// eight chunks and at most 35 squarings, or an immediate zero at 9,000 years.
pub fn net_decay(mut amount: U256, elapsed: U256) -> Result<U256, NetMathError> {
    if amount == U256::ZERO || elapsed >= U256::from_u128(EMPTY_SECONDS as u128) {
        return Ok(U256::ZERO);
    }
    let mut seconds = elapsed.as_u128().unwrap() as u64;
    while seconds >= CHUNK_SECONDS {
        amount = scaled(amount, CHUNK_FACTOR)?;
        if amount == U256::ZERO {
            return Ok(U256::ZERO);
        }
        seconds -= CHUNK_SECONDS;
    }
    let mut factor = NET_SCALE;
    let mut base = SECOND_FACTOR;
    while seconds != 0 {
        if seconds & 1 != 0 {
            factor = crate::mul_div_wide(factor, base, NET_SCALE);
        }
        seconds >>= 1;
        if seconds != 0 {
            base = crate::mul_div_wide(base, base, NET_SCALE);
        }
    }
    scaled(amount, factor)
}

/// Pre: fund <= issuance, in identical net units. Post: fund only gains the
/// loss of outside balances; fund_after + outside_after == issuance exactly.
pub fn net_fund_after(issuance: U256, fund: U256, elapsed: U256) -> Result<U256, NetMathError> {
    if fund > issuance {
        return Err(NetMathError::FundExceedsIssuance);
    }
    let outside = issuance.checked_sub(fund)?;
    issuance.checked_sub(net_decay(outside, elapsed)?)
}

/// Pre: principal > 0 and reduction <= principal. Post: target <= reduction.
/// This is a baseline, not a promise of liquid or marked Jubilee relief.
pub fn net_target_relief(
    principal: U256,
    buck_seconds: U256,
    reduction: U256,
) -> Result<U256, NetMathError> {
    if principal == U256::ZERO || reduction > principal {
        return Err(NetMathError::InvalidPrincipal);
    }
    let selected = mul_div_256(buck_seconds, reduction, principal, false)?;
    let target = mul_div_256(
        selected,
        U256::ONE,
        U256::from_u128(RELIEF_SECONDS as u128),
        false,
    )?;
    Ok(target.min(reduction))
}
