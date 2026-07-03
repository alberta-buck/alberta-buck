//! `import buck_math` -- the Python face of the buck-math kernel.
//!
//! Thin PyO3 wrappers; every number crosses as a native Python int
//! (PyO3 maps u128/i128 losslessly).  Semantics, panics and all, are
//! the kernel's -- overflow/domain panics surface as PanicException,
//! where the contracts revert.

use pyo3::prelude::*;

#[pyfunction]
fn depreciate(face: u128, dep_type: u8, rate_bp: u32, floor: u128,
              start_at: u64, now: u64) -> u128 {
    kernel::depreciate(face, kernel::DepType::from_u8(dep_type),
                          rate_bp, floor, start_at, now)
}

#[pyfunction]
fn current_value(face: u128, dep_type: u8, rate_bp: u32, floor: u128,
                 start_at: u64, now: u64, activated: u128) -> u128 {
    kernel::current_value(face, kernel::DepType::from_u8(dep_type),
                             rate_bp, floor, start_at, now, activated)
}

#[pyfunction]
fn fee_owing(buck_seconds: u128, raw: i128, elapsed: u64) -> u128 {
    kernel::fee_owing(buck_seconds, raw, elapsed)
}

/// Returns (carried, from_raw, from_bs, to_raw, to_bs).
#[pyfunction]
fn carrying_transfer(from_raw: i128, from_bs: u128, from_elapsed: u64,
                     to_raw: i128, to_bs: u128, to_elapsed: u64,
                     value: u128) -> (u128, i128, u128, i128, u128) {
    let r = kernel::carrying_transfer(from_raw, from_bs, from_elapsed,
                                         to_raw, to_bs, to_elapsed, value);
    (r.carried, r.from_raw, r.from_bs, r.to_raw, r.to_bs)
}

#[pyclass]
struct DirectPid(kernel::DirectPid);

#[pymethods]
impl DirectPid {
    #[new]
    fn new(kp: i128, ki: i128, kd: i128, dt: u64,
           k_min: u128, k_max: u128, buck_k: u128, now: u64) -> Self {
        DirectPid(kernel::DirectPid::new(kp, ki, kd, dt, k_min, k_max,
                                            buck_k, now))
    }

    fn compute(&mut self, now: u64, basket_value: i128) -> u128 {
        self.0.compute(now, basket_value)
    }

    fn funding_factor(&self) -> u128 { self.0.funding_factor() }
    fn rederive_i(&self) -> i128 { self.0.rederive_i() }
    fn retune(&mut self, kp: i128, ki: i128, kd: i128) {
        self.0.retune(kp, ki, kd)
    }
    fn set_buck_k0(&mut self, k0: u128) { self.0.set_buck_k0(k0) }
    fn set_rails(&mut self, k_min: u128, k_max: u128) {
        self.0.set_rails(k_min, k_max)
    }
    fn reprime(&mut self, now: u64, basket_value: i128) {
        self.0.reprime(now, basket_value)
    }

    #[getter] fn p(&self) -> i128 { self.0.p }
    #[getter] fn i(&self) -> i128 { self.0.i }
    #[getter] fn d(&self) -> i128 { self.0.d }
    #[getter] fn buck_k(&self) -> u128 { self.0.buck_k }
    #[getter] fn buck_k0(&self) -> u128 { self.0.buck_k0 }
    #[getter] fn last_update(&self) -> u64 { self.0.last_update }
    #[getter] fn dt_max(&self) -> u64 { self.0.dt_max }
    #[setter] fn set_dt_max(&mut self, v: u64) { self.0.dt_max = v }
    #[getter] fn last_basket_cost(&self) -> i128 { self.0.last_basket_cost }
    #[setter] fn set_last_basket_cost(&mut self, v: i128) {
        self.0.last_basket_cost = v
    }
    #[getter] fn last_buck_price(&self) -> i128 { self.0.last_buck_price }
    #[setter] fn set_last_buck_price(&mut self, v: i128) {
        self.0.last_buck_price = v
    }
}

#[pymodule]
fn buck_math(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(depreciate, m)?)?;
    m.add_function(wrap_pyfunction!(current_value, m)?)?;
    m.add_function(wrap_pyfunction!(fee_owing, m)?)?;
    m.add_function(wrap_pyfunction!(carrying_transfer, m)?)?;
    m.add_class::<DirectPid>()?;
    m.add("BP", kernel::BP)?;
    m.add("SECONDS_PER_YEAR", kernel::SECONDS_PER_YEAR)?;
    m.add("BASE_RATE_PER_SEC", kernel::BASE_RATE_PER_SEC)?;
    m.add("SCALE", kernel::SCALE)?;
    m.add("MAX_BS", kernel::MAX_BS)?;
    m.add("UNIT", kernel::UNIT)?;
    Ok(())
}
