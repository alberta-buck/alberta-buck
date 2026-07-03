//! BuckKControllerDirect -- the exact rescaled-ppm PID from
//! src/BuckKControllerDirect.sol (with BuckKControllerBase's
//! fundingFactor and governance algebra).
//!
//! The loop runs in ppm process/error and seconds time; gains are stored
//! as real_gain * 1e12 so outputs land directly in 1e18 buckK units:
//!
//! ```text
//! err   = buckValue/1e12 - basketValue/1e12          (ppm)
//! buckK = clamp(buckK0 + Kp*err + Ki*I + Kd*dErr/dt, kmin, kmax)
//! ```
//!
//! All arithmetic is i128/u128 with overflow checks on (the workspace
//! profile), matching Solidity 0.8 checked int256 within the realistic
//! domain (|values| < 2^127); out-of-domain panics where Solidity would
//! keep going, which the golden vectors never exercise.

use crate::{TO18, UNIT};

const PPM_PARITY: i128 = UNIT / TO18; // 1_000_000 -- 1.0 in ppm

#[derive(Clone, Debug)]
pub struct DirectPid {
    pub kp: i128,
    pub ki: i128,
    pub kd: i128,
    pub p: i128,
    pub i: i128,
    pub d: i128,
    pub last_update: u64,
    pub last_basket_cost: i128, // ppm in the direct embodiment
    pub last_buck_price: i128,  // ppm
    pub buck_k: u128,
    pub buck_k0: u128,
    pub dt: u64,
    pub dt_max: u64,
    pub k_min: u128,
    pub k_max: u128,
}

impl DirectPid {
    /// Mirror of the BuckKControllerDirect constructor: feed-forward from
    /// the initial K, integrator primed to 0 (parity), refs at ppm parity.
    pub fn new(kp: i128, ki: i128, kd: i128, dt: u64,
               k_min: u128, k_max: u128, buck_k: u128, now: u64) -> DirectPid {
        assert!(k_min <= buck_k && buck_k <= k_max, "buckK out of bounds");
        DirectPid {
            kp, ki, kd,
            p: 0, i: 0, d: 0,
            last_update: now,
            last_basket_cost: PPM_PARITY,
            last_buck_price: PPM_PARITY,
            buck_k,
            buck_k0: buck_k,
            dt,
            dt_max: u64::MAX,
            k_min, k_max,
        }
    }

    /// One PID cycle at time `now` against `basket_value` (18-dec, as
    /// basketValueInBuck() returns).  Returns the (possibly cached) buckK.
    pub fn compute(&mut self, now: u64, basket_value: i128) -> u128 {
        let elapsed = now - self.last_update;
        if elapsed < self.dt {
            return self.buck_k;
        }

        let setpoint = UNIT / TO18; // constant 1.0 BUCK, in ppm
        let process = basket_value / TO18;
        let err = setpoint - process;

        let effective = if elapsed > self.dt_max { self.dt_max } else { elapsed };
        let dt = effective as i128;

        let new_i = self.i + err * dt; // ppm*seconds
        let d_err = err - self.p;

        let u_p = self.kp * err;
        let u_i = self.ki * new_i;
        let u_d = if dt > 0 { self.kd * d_err / dt } else { 0 };

        let raw_output = self.buck_k0 as i128 + u_p + u_i + u_d;

        // Anti-windup: at a rail, only let the integrator move back toward
        // the operating band.
        let new_buck_k;
        if raw_output < self.k_min as i128 {
            new_buck_k = self.k_min;
            if new_i > self.i {
                self.i = new_i;
            }
        } else if raw_output > self.k_max as i128 {
            new_buck_k = self.k_max;
            if new_i < self.i {
                self.i = new_i;
            }
        } else {
            new_buck_k = raw_output as u128;
            self.i = new_i;
        }

        self.p = err;
        self.d = d_err;
        self.buck_k = new_buck_k;
        self.last_update = now;
        self.last_basket_cost = process;
        self.last_buck_price = setpoint;
        new_buck_k
    }

    /// Counter-cyclical insurance funding factor (18-dec; 1e18 = 1.0),
    /// from BuckKControllerBase.fundingFactor().  Scale-free in the
    /// (basket - buck)/basket ratio, so ppm references work unchanged.
    pub fn funding_factor(&self) -> u128 {
        let b = self.last_basket_cost;
        if b <= 0 {
            return UNIT as u128;
        }
        let p = self.last_buck_price;
        let raw = UNIT + 10 * (b - p) * UNIT / b;
        if raw <= 0 {
            0
        } else {
            raw as u128
        }
    }

    /// Bumpless-transfer integrator (Direct._rederiveI): the I holding the
    /// current output under the current gains and last error.
    pub fn rederive_i(&self) -> i128 {
        (self.buck_k as i128 - self.buck_k0 as i128 - self.kp * self.p) / self.ki
    }

    /// Governance retune with bumpless transfer (base.retune).
    pub fn retune(&mut self, kp: i128, ki: i128, kd: i128) {
        assert!(ki != 0, "retune needs Ki");
        self.kp = kp;
        self.ki = ki;
        self.kd = kd;
        self.i = self.rederive_i();
    }

    /// Governance: move the neutral feed-forward LTV, bumpless
    /// (Direct.setBuckK0).
    pub fn set_buck_k0(&mut self, k0: u128) {
        assert!(self.k_min <= k0 && k0 <= self.k_max, "buckK0 out of bounds");
        self.buck_k0 = k0;
        if self.ki != 0 {
            self.i = self.rederive_i();
        }
    }

    /// Governance: move the rails; live buckK is clamped in (base.setRails).
    pub fn set_rails(&mut self, k_min: u128, k_max: u128) {
        assert!(k_min <= k_max, "min>max");
        self.k_min = k_min;
        self.k_max = k_max;
        self.buck_k = self.buck_k.clamp(k_min, k_max);
    }

    /// Absorb a process discontinuity (Direct.reprime): recapture P and
    /// re-derive I so the next no-motion cycle reproduces the current buckK.
    pub fn reprime(&mut self, now: u64, basket_value: i128) {
        let setpoint = UNIT / TO18;
        let process = basket_value / TO18;
        let err = setpoint - process;
        self.p = err;
        self.d = 0;
        if self.ki != 0 {
            self.i = (self.buck_k as i128 - self.buck_k0 as i128
                      - self.kp * err) / self.ki;
        }
        self.last_basket_cost = process;
        self.last_buck_price = setpoint;
        self.last_update = now;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pid() -> DirectPid {
        DirectPid::new(5_000_000_000, 2_000_000, 0, 600,
                       0, 950_000_000_000_000_000, 750_000_000_000_000_000, 0)
    }

    #[test]
    fn cached_within_dt() {
        let mut c = pid();
        assert_eq!(c.compute(599, 1_100_000_000_000_000_000), c.buck_k);
        assert_eq!(c.last_update, 0); // untouched
    }

    #[test]
    fn parity_holds_k0() {
        let mut c = pid();
        assert_eq!(c.compute(3600, UNIT), 750_000_000_000_000_000);
    }

    #[test]
    fn retune_is_bumpless() {
        let mut c = pid();
        c.compute(3600, 1_050_000_000_000_000_000); // wind up some I
        let k = c.compute(7200, 1_050_000_000_000_000_000) as i128;
        c.retune(c.kp / 2, c.ki * 2, 0);
        // The re-derived I holds the output at the retune instant (the
        // integrator keeps integrating on subsequent cycles): the output
        // reconstruction k0 + Kp*P + Ki*I matches buckK to within the
        // rederive division's truncation (< Ki).
        let reconstructed = c.buck_k0 as i128 + c.kp * c.p + c.ki * c.i;
        assert!((k - reconstructed).abs() < c.ki,
                "not bumpless: k={k} reconstructed={reconstructed}");
    }
}
