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
//!
//! buckSeconds is read by sign.  Above zero it counts fee-seconds, as
//! above; below zero (a lien: credit drawn) it counts issuance-seconds,
//! the lien integrated over time, on which the holder's Jubilee relief
//! accrues at the same 2%/year.  BUCK arriving at an account below zero
//! repay its lien net of the fee they carry, and a receipt that repays the
//! whole lien pays the relief out with it (doc/JUBILEE-ISSUANCE.org).

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
/// crystallisation.  A recipient at or above zero takes the BUCK and their
/// age in.  A recipient below zero first crystallises its issuance-seconds,
/// then is repaid `value - fee` (the fee the carried age owes); if that
/// repays the whole lien, the relief its issuance-seconds earned is paid
/// out too (capped at the lien) and its buckSeconds restart at zero.
/// Mirrors Buck._carryingTransfer / _credit, assuming the Jubilee fund
/// covers the relief -- which it does by construction, accruing on the BUCK
/// issued.
pub fn carrying_transfer(from_raw: i128, from_bs: u128, from_elapsed: u64,
                         to_raw: i128, to_bs: u128, to_elapsed: u64,
                         value: u128) -> CarryingResult {
    assert!(from_raw >= value as i128, "BUCK: Carrying amount exceeds raw");
    let raw = from_raw as u128;

    let live_bs = from_bs + raw * from_elapsed as u128;
    let carried = if raw > 0 { mul_div_wide(live_bs, value, raw) } else { 0 };

    let from_raw_after = to_buck_qty_signed((raw - value) as i128);
    let from_bs_after = to_buck_seconds(live_bs - carried);

    let (to_raw_after, to_bs_after) = if to_raw >= 0 {
        // A holder: the age rides in.
        let to_live = to_bs + to_raw as u128 * to_elapsed as u128;
        (to_buck_qty_signed(to_raw + value as i128),
         to_buck_seconds(to_live + carried))
    } else {
        // A lien: issuance-seconds through now, then repaid net of the fee.
        let lien = (-to_raw) as u128;
        let to_live = to_buck_seconds(to_bs + lien * to_elapsed as u128);
        let fee = mul_div_wide(carried, BASE_RATE_PER_SEC, SCALE).min(value);
        let net = to_raw + (value - fee) as i128;
        if net >= 0 {
            let relief = mul_div_wide(to_live, BASE_RATE_PER_SEC, SCALE).min(lien);
            (to_buck_qty_signed(net + relief as i128), 0)
        } else {
            (to_buck_qty_signed(net), to_live)
        }
    };

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

    #[test]
    fn a_lien_is_repaid_net_of_the_fee_and_crossing_pays_its_relief() {
        // 1e12 raw aged a day into a lien of 3e11 carried a year.
        let y = SECONDS_PER_YEAR as u128;
        let r = carrying_transfer(1_000_000_000_000, 0, 86_400,
                                  -300_000_000_000, 300_000_000_000 * y, 0,
                                  1_000_000_000_000);
        let fee = mul_div_wide(r.carried, BASE_RATE_PER_SEC, SCALE);
        let relief = mul_div_wide(300_000_000_000 * y, BASE_RATE_PER_SEC, SCALE);
        assert_eq!(r.to_raw, 700_000_000_000 - fee as i128 + relief as i128);
        assert_eq!(r.to_bs, 0, "crossed: the age restarts");
    }
}
