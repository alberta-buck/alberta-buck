//! BuckCredit depreciation -- the exact `_depreciate` / `currentValue`
//! algebra from src/BuckCredit.sol.  Discrete whole-year declining-balance
//! compounding with linear interpolation across the trailing partial year;
//! no transcendental approximations.

use crate::wide::mul_div_wide;
use crate::{BP, MAX_DEP_YEARS, SECONDS_PER_YEAR};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum DepType {
    None = 0,
    Linear = 1,
    DecliningBalance = 2,
}

impl DepType {
    pub fn from_u8(v: u8) -> DepType {
        match v {
            0 => DepType::None,
            1 => DepType::Linear,
            2 => DepType::DecliningBalance,
            _ => panic!("unknown DepreciationType {v}"),
        }
    }
}

/// Depreciated face value at time `now` (BuckCredit._depreciate).
///
/// Domains match the contract's packed fields: face/floor fit uint80,
/// rate_bp fits uint32, start_at fits uint48 -- all comfortably inside
/// the u128 arithmetic here (the one wide product uses mul_div_wide).
pub fn depreciate(face: u128, dep_type: DepType, rate_bp: u32, floor: u128,
                  start_at: u64, now: u64) -> u128 {
    if dep_type == DepType::None || now <= start_at {
        return face;
    }
    if face <= floor {
        return floor;
    }
    let elapsed = (now - start_at) as u128;
    let depreciable = face - floor;
    let rate = rate_bp as u128;

    match dep_type {
        DepType::Linear => {
            let loss =
                mul_div_wide(depreciable * rate, elapsed, SECONDS_PER_YEAR * BP);
            if loss >= depreciable {
                floor
            } else {
                face - loss
            }
        }
        DepType::DecliningBalance => {
            if rate == 0 {
                return face;
            }
            if rate >= BP {
                return floor;
            }
            let whole_years = elapsed / SECONDS_PER_YEAR;
            if whole_years >= MAX_DEP_YEARS {
                return floor;
            }
            let keep = BP - rate;
            let mut v = depreciable;
            for _ in 0..whole_years {
                v = v * keep / BP;
                if v == 0 {
                    return floor;
                }
            }
            let frac_sec = elapsed - whole_years * SECONDS_PER_YEAR;
            if frac_sec != 0 {
                let v_next = v * keep / BP;
                v -= (v - v_next) * frac_sec / SECONDS_PER_YEAR;
            }
            floor + v
        }
        DepType::None => unreachable!(),
    }
}

/// Current value of the ACTIVATED portion (BuckCredit.currentValue):
/// the depreciated face, scaled by activated/face.
pub fn current_value(face: u128, dep_type: DepType, rate_bp: u32,
                     floor: u128, start_at: u64, now: u64,
                     activated: u128) -> u128 {
    if activated == 0 {
        return 0;
    }
    let dep = depreciate(face, dep_type, rate_bp, floor, start_at, now);
    mul_div_wide(dep, activated, face)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn none_and_pre_start_hold_face() {
        assert_eq!(depreciate(1000, DepType::None, 5000, 0, 100, 200), 1000);
        assert_eq!(depreciate(1000, DepType::Linear, 5000, 0, 200, 200), 1000);
    }

    #[test]
    fn activated_portion_scales_proportionally() {
        // One whole declining year at 15%: 1e6 -> 850_000; a third activated.
        let spy = SECONDS_PER_YEAR as u64;
        assert_eq!(current_value(1_000_000, DepType::DecliningBalance, 1500,
                                 0, 0, spy, 333_333),
                   283_333); // floor(850_000 * 333_333 / 1_000_000)
    }
}
