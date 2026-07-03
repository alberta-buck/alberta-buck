//! Buck demurrage -- the exact `_feeOwing` / `_carryingTransfer`
//! buckSeconds algebra from src/Buck.sol.
//!
//! buckSeconds is the cumulative integral of (balance * dt), crystallised
//! through each account's timestamp; the live value adds the current
//! rectangle.  The demurrage fee is that integral at 2%/year:
//!
//! ```text
//! fee = (buckSeconds + raw * elapsed) * BASE_RATE_PER_SEC / SCALE
//! ```
//!
//! Carrying transfers apportion the sender's live buckSeconds to the
//! recipient in proportion value/raw, so demurrage follows the BUCKs.

use crate::wide::mul_div_wide;
use crate::{BASE_RATE_PER_SEC, MAX_BALANCE_SIGNED, MAX_BS, SCALE};

/// Demurrage owed by an account with `buck_seconds` crystallised history
/// and signed raw balance `raw`, `elapsed` seconds after its last
/// crystallisation.  Mirrors the PUBLIC Buck.feeOwing(address) view,
/// including its gate: no demurrage on used credit or empty accounts
/// (raw <= 0), regardless of crystallised history.
pub fn fee_owing(buck_seconds: u128, raw: i128, elapsed: u64) -> u128 {
    if raw <= 0 {
        return 0;
    }
    let live = buck_seconds + raw as u128 * elapsed as u128;
    if live == 0 {
        return 0;
    }
    mul_div_wide(live, BASE_RATE_PER_SEC, SCALE)
}

/// Post-state of a carrying transfer (Buck._carryingTransfer).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct CarryingResult {
    pub carried: u128,        // buckSeconds moved with the value
    pub from_raw: i128,
    pub from_bs: u128,
    pub to_raw: i128,
    pub to_bs: u128,
}

fn to_buck_seconds(x: u128) -> u128 {
    assert!(x <= MAX_BS, "BuckSeconds: overflow");
    x
}

fn to_buck_qty_signed(x: i128) -> i128 {
    assert!((-MAX_BALANCE_SIGNED - 1..=MAX_BALANCE_SIGNED).contains(&x),
            "BuckQty: out of range");
    x
}

/// Apportion the sender's live buckSeconds across a transfer of `value`.
///
/// `from_elapsed` / `to_elapsed` are the seconds since each side's last
/// crystallisation.  The recipient may be negative (credit drawn); its
/// buckSeconds rectangle uses only the positive portion of its history.
pub fn carrying_transfer(from_raw: i128, from_bs: u128, from_elapsed: u64,
                         to_raw: i128, to_bs: u128, to_elapsed: u64,
                         value: u128) -> CarryingResult {
    assert!(from_raw >= value as i128, "BUCK: Carrying amount exceeds raw");
    let raw = from_raw as u128;

    let live_bs = from_bs + raw * from_elapsed as u128;
    let carried = if raw > 0 { mul_div_wide(live_bs, value, raw) } else { 0 };

    let from_raw_after = to_buck_qty_signed((raw - value) as i128);
    let from_bs_after = to_buck_seconds(live_bs - carried);

    let to_raw_pos = if to_raw > 0 { to_raw as u128 } else { 0 };
    let to_raw_after = to_buck_qty_signed(to_raw + value as i128);
    let to_bs_after =
        to_buck_seconds(to_bs + to_raw_pos * to_elapsed as u128 + carried);

    CarryingResult {
        carried,
        from_raw: from_raw_after,
        from_bs: from_bs_after,
        to_raw: to_raw_after,
        to_bs: to_bs_after,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::SECONDS_PER_YEAR;

    #[test]
    fn one_buck_one_year_is_two_percent() {
        // The Buck.sol doc example: 1 BUCK (1e6 raw) held 1 year -> 20_000.
        // (19_999 exactly: BASE_RATE_PER_SEC floors 2e25/31_557_600.)
        let fee = fee_owing(0, 1_000_000, SECONDS_PER_YEAR as u64);
        assert!((19_999..=20_000).contains(&fee), "fee={fee}");
    }

    #[test]
    fn apportionment_conserves_buck_seconds() {
        let r = carrying_transfer(1_000, 5_000, 10, 0, 0, 0, 400);
        // live = 5_000 + 10_000 = 15_000; carried = 15_000*400/1_000 = 6_000.
        assert_eq!(r.carried, 6_000);
        assert_eq!(r.from_bs + r.carried, 15_000);
        assert_eq!(r.to_bs, 6_000);
        assert_eq!((r.from_raw, r.to_raw), (600, 400));
    }
}
