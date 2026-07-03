//! The JS/WASM face of the buck-math kernel (browser + Node; numbers
//! cross as BigInt).  Semantics, panics and all, are the kernel's --
//! overflow/domain panics surface as WASM traps, where the contracts
//! revert.
//!
//! Built by `make nix-core-build-wasm` (wasm-pack, nodejs target) into
//! core/js/wasm/; the JS suite asserts the golden vectors through it.

use wasm_bindgen::prelude::*;

#[wasm_bindgen]
pub fn depreciate(face: u128, dep_type: u8, rate_bp: u32, floor: u128,
                  start_at: u64, now: u64) -> u128 {
    kernel::depreciate(face, kernel::DepType::from_u8(dep_type), rate_bp,
                       floor, start_at, now)
}

#[wasm_bindgen]
pub fn current_value(face: u128, dep_type: u8, rate_bp: u32, floor: u128,
                     start_at: u64, now: u64, activated: u128) -> u128 {
    kernel::current_value(face, kernel::DepType::from_u8(dep_type), rate_bp,
                          floor, start_at, now, activated)
}

#[wasm_bindgen]
pub fn fee_owing(buck_seconds: u128, raw: i128, elapsed: u64) -> u128 {
    kernel::fee_owing(buck_seconds, raw, elapsed)
}

/// Carrying-transfer post-state; fields cross as BigInt.
#[wasm_bindgen(getter_with_clone)]
pub struct CarryingResult {
    pub carried: u128,
    pub from_raw: i128,
    pub from_bs: u128,
    pub to_raw: i128,
    pub to_bs: u128,
}

#[wasm_bindgen]
pub fn carrying_transfer(from_raw: i128, from_bs: u128, from_elapsed: u64,
                         to_raw: i128, to_bs: u128, to_elapsed: u64,
                         value: u128) -> CarryingResult {
    let r = kernel::carrying_transfer(from_raw, from_bs, from_elapsed,
                                      to_raw, to_bs, to_elapsed, value);
    CarryingResult {
        carried: r.carried,
        from_raw: r.from_raw,
        from_bs: r.from_bs,
        to_raw: r.to_raw,
        to_bs: r.to_bs,
    }
}

#[wasm_bindgen]
pub struct DirectPid(kernel::DirectPid);

#[wasm_bindgen]
impl DirectPid {
    #[wasm_bindgen(constructor)]
    pub fn new(kp: i128, ki: i128, kd: i128, dt: u64,
               k_min: u128, k_max: u128, buck_k: u128, now: u64) -> DirectPid {
        DirectPid(kernel::DirectPid::new(kp, ki, kd, dt, k_min, k_max,
                                         buck_k, now))
    }

    pub fn compute(&mut self, now: u64, basket_value: i128) -> u128 {
        self.0.compute(now, basket_value)
    }

    pub fn funding_factor(&self) -> u128 { self.0.funding_factor() }
    pub fn rederive_i(&self) -> i128 { self.0.rederive_i() }
    pub fn retune(&mut self, kp: i128, ki: i128, kd: i128) {
        self.0.retune(kp, ki, kd)
    }
    pub fn set_buck_k0(&mut self, k0: u128) { self.0.set_buck_k0(k0) }
    pub fn set_rails(&mut self, k_min: u128, k_max: u128) {
        self.0.set_rails(k_min, k_max)
    }
    pub fn reprime(&mut self, now: u64, basket_value: i128) {
        self.0.reprime(now, basket_value)
    }

    #[wasm_bindgen(getter)] pub fn p(&self) -> i128 { self.0.p }
    #[wasm_bindgen(getter)] pub fn i(&self) -> i128 { self.0.i }
    #[wasm_bindgen(getter)] pub fn d(&self) -> i128 { self.0.d }
    #[wasm_bindgen(getter)] pub fn buck_k(&self) -> u128 { self.0.buck_k }
    #[wasm_bindgen(getter)] pub fn buck_k0(&self) -> u128 { self.0.buck_k0 }
    #[wasm_bindgen(getter)] pub fn last_update(&self) -> u64 {
        self.0.last_update
    }
    #[wasm_bindgen(getter)] pub fn dt_max(&self) -> u64 { self.0.dt_max }
    #[wasm_bindgen(setter)] pub fn set_dt_max(&mut self, v: u64) {
        self.0.dt_max = v;
    }
    #[wasm_bindgen(getter)] pub fn last_basket_cost(&self) -> i128 {
        self.0.last_basket_cost
    }
    #[wasm_bindgen(setter)] pub fn set_last_basket_cost(&mut self, v: i128) {
        self.0.last_basket_cost = v;
    }
    #[wasm_bindgen(getter)] pub fn last_buck_price(&self) -> i128 {
        self.0.last_buck_price
    }
    #[wasm_bindgen(setter)] pub fn set_last_buck_price(&mut self, v: i128) {
        self.0.last_buck_price = v;
    }
}
