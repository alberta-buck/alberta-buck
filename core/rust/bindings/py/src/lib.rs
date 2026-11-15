//! `import buck_math` -- the Python face of the buck-math kernel.
//!
//! Thin PyO3 wrappers: canonical uint256 values cross as native Python int
//! via lossless bytes; domain/quotient errors raise ValueError. Existing
//! u128/i128 comparison functions retain PyO3's scalar mapping and their
//! legacy overflow/domain PanicException behavior.

use pyo3::prelude::*;
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::types::{PyBool, PyBytes, PyInt};

// Python ints stay ints: to_bytes rejects negatives and values above uint256,
// and from_bytes returns the entire result. No float, string or u128 cast.
fn uint256(value: &Bound<'_, PyInt>) -> PyResult<kernel::U256> {
    if value.is_instance_of::<PyBool>() {
        return Err(PyTypeError::new_err("uint256 requires an integer, not bool"));
    }
    let bytes: Vec<u8> = value.py().get_type::<PyInt>()
        .call_method1("to_bytes", (value, 32, "big"))?.extract()?;
    Ok(kernel::U256::from_be_bytes(bytes.try_into().unwrap()))
}

fn net_result(py: Python<'_>, result: Result<kernel::U256, kernel::NetMathError>) -> PyResult<Py<PyAny>> {
    let value = result.map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(py.get_type::<PyInt>().call_method1("from_bytes", (PyBytes::new(py, &value.to_be_bytes()), "big"))?.unbind())
}

#[pyfunction]
fn net_decay(amount: &Bound<'_, PyInt>, elapsed: &Bound<'_, PyInt>) -> PyResult<Py<PyAny>> {
    net_result(amount.py(), kernel::net_decay(uint256(amount)?, uint256(elapsed)?))
}

#[pyfunction]
fn net_fund_after(issuance: &Bound<'_, PyInt>, fund: &Bound<'_, PyInt>, elapsed: &Bound<'_, PyInt>) -> PyResult<Py<PyAny>> {
    net_result(issuance.py(), kernel::net_fund_after(uint256(issuance)?, uint256(fund)?, uint256(elapsed)?))
}

#[pyfunction]
fn net_target_relief(principal: &Bound<'_, PyInt>, buck_seconds: &Bound<'_, PyInt>, reduction: &Bound<'_, PyInt>) -> PyResult<Py<PyAny>> {
    net_result(principal.py(), kernel::net_target_relief(uint256(principal)?, uint256(buck_seconds)?, uint256(reduction)?))
}

#[pyfunction]
#[pyo3(signature = (a, b, denominator, round_up=false))]
fn mul_div_256(a: &Bound<'_, PyInt>, b: &Bound<'_, PyInt>, denominator: &Bound<'_, PyInt>, round_up: bool) -> PyResult<Py<PyAny>> {
    net_result(a.py(), kernel::mul_div_256(uint256(a)?, uint256(b)?, uint256(denominator)?, round_up))
}

// Carried BUCK (src/lib/CarryRatio.sol): an account's (gross, net) and an amount or price.
#[pyfunction]
fn carry_to_net(gross: &Bound<'_, PyInt>, net: &Bound<'_, PyInt>, nominal: &Bound<'_, PyInt>) -> PyResult<Py<PyAny>> {
    net_result(gross.py(), kernel::carry_to_net(uint256(gross)?, uint256(net)?, uint256(nominal)?))
}

#[pyfunction]
fn carry_to_net_scaled(gross: &Bound<'_, PyInt>, net: &Bound<'_, PyInt>, nominal: &Bound<'_, PyInt>) -> PyResult<Py<PyAny>> {
    net_result(gross.py(), kernel::carry_to_net_scaled(uint256(gross)?, uint256(net)?, uint256(nominal)?))
}

#[pyfunction]
fn carry_to_nominal(gross: &Bound<'_, PyInt>, net: &Bound<'_, PyInt>, value: &Bound<'_, PyInt>) -> PyResult<Py<PyAny>> {
    net_result(gross.py(), kernel::carry_to_nominal(uint256(gross)?, uint256(net)?, uint256(value)?))
}

// A pool's price at a tick (OracleLibrary.getQuoteAtTick): `base` of the base token in the quote
// token; `base_is_token0` when the base token's address is the lower.
#[pyfunction]
fn quote_at_tick(tick: i32, base: &Bound<'_, PyInt>, base_is_token0: bool) -> PyResult<Py<PyAny>> {
    let amount = uint256(base)?.as_u128().ok_or_else(|| PyValueError::new_err("base amount above uint128"))?;
    net_result(base.py(), kernel::quote_at_tick(tick, amount, base_is_token0))
}

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
    m.add_function(wrap_pyfunction!(net_decay, m)?)?;
    m.add_function(wrap_pyfunction!(net_fund_after, m)?)?;
    m.add_function(wrap_pyfunction!(net_target_relief, m)?)?;
    m.add_function(wrap_pyfunction!(mul_div_256, m)?)?;
    m.add_function(wrap_pyfunction!(carry_to_net, m)?)?;
    m.add_function(wrap_pyfunction!(carry_to_net_scaled, m)?)?;
    m.add_function(wrap_pyfunction!(carry_to_nominal, m)?)?;
    m.add_function(wrap_pyfunction!(quote_at_tick, m)?)?;
    m.add("NET_SCALE", kernel::net_demurrage::NET_SCALE)?;
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
