//! PyO3 binding for the buck-identity kernel: `import buck_core.buck_identity`.
//!
//! Everything crosses the boundary as plain Python ints (num-bigint), in
//! the wallet's word convention: scalars/coordinates are uint256; a G1
//! point is an `(x, y)` tuple with `(0, 0)` the point at infinity; a G2
//! point is `((x_c0, x_c1), (y_c0, y_c1))`; an ElGamal ciphertext is a
//! `(R, C)` pair of G1 tuples.  Proofs are flat tuples in the Python
//! dataclass field order -- the `alberta_buck.wallet` shims wrap them.
//!
//! Deterministic by construction: every nonce is an argument (the shims
//! draw them, preserving the pinned rng streams).  Kernel errors surface
//! as `ValueError` with the reference implementation's message.

use num_bigint::BigUint;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use kernel::{G1w, G2w, IdError, W256};

type PyG1 = (BigUint, BigUint);
type PyG2 = ((BigUint, BigUint), (BigUint, BigUint));
type PyCt = (PyG1, PyG1);
type PyReg = (BigUint, BigUint, BigUint, PyG1, PyG1, PyG1);
type PyCp = (BigUint, BigUint, BigUint, PyG1, PyG1, PyG1);
type PyVd = (BigUint, BigUint, PyG1, PyG1);
// PyO3 tuples cap at 12 elements; the 13-field issuer-reenc proof nests as
// ((e, s_r, s_b, s_s, s_g), (A1, A2, A3, A4, A5, Q, U, T)).
#[allow(clippy::type_complexity)]
type PyIr = (
    (BigUint, BigUint, BigUint, BigUint, BigUint),
    (PyG1, PyG1, PyG1, PyG1, PyG1, PyG1, PyG1, PyG1),
);
type PyDc = (BigUint, BigUint, BigUint, BigUint, PyG1, PyG1, PyG1, PyG1);
#[allow(clippy::type_complexity)]
type PyDb = (
    BigUint, BigUint, BigUint, BigUint, BigUint,
    PyG1, PyG1, PyG1, PyG1, PyG1, PyG1,
);

// ---------------------------------------------------------------------------
// int <-> word conversions
// ---------------------------------------------------------------------------

fn w(v: &BigUint) -> PyResult<W256> {
    let bytes = v.to_bytes_be();
    if bytes.len() > 32 {
        return Err(PyValueError::new_err("word out of range (>= 2^256)"));
    }
    let mut out = [0u8; 32];
    out[32 - bytes.len()..].copy_from_slice(&bytes);
    Ok(out)
}

fn big(x: &W256) -> BigUint {
    BigUint::from_bytes_be(x)
}

fn wg1(p: &PyG1) -> PyResult<G1w> {
    Ok((w(&p.0)?, w(&p.1)?))
}

fn pyg1(p: &G1w) -> PyG1 {
    (big(&p.0), big(&p.1))
}

fn wg2(p: &PyG2) -> PyResult<G2w> {
    Ok(((w(&p.0 .0)?, w(&p.0 .1)?), (w(&p.1 .0)?, w(&p.1 .1)?)))
}

fn pyg2(p: &G2w) -> PyG2 {
    (
        (big(&p.0 .0), big(&p.0 .1)),
        (big(&p.1 .0), big(&p.1 .1)),
    )
}

fn wct(ct: &PyCt) -> PyResult<(G1w, G1w)> {
    Ok((wg1(&ct.0)?, wg1(&ct.1)?))
}

fn pyct(ct: &(G1w, G1w)) -> PyCt {
    (pyg1(&ct.0), pyg1(&ct.1))
}

fn err(e: IdError) -> PyErr {
    PyValueError::new_err(e.0)
}

// ---------------------------------------------------------------------------
// Curve ops / hashes
// ---------------------------------------------------------------------------

#[pyfunction]
fn g1_add(a: PyG1, b: PyG1) -> PyResult<PyG1> {
    Ok(pyg1(&kernel::g1_add(&wg1(&a)?, &wg1(&b)?).map_err(err)?))
}

#[pyfunction]
fn g1_mul(p: PyG1, k: BigUint) -> PyResult<PyG1> {
    Ok(pyg1(&kernel::g1_mul(&wg1(&p)?, &w(&k)?).map_err(err)?))
}

#[pyfunction]
fn g1_neg(p: PyG1) -> PyResult<PyG1> {
    Ok(pyg1(&kernel::g1_neg(&wg1(&p)?).map_err(err)?))
}

#[pyfunction]
fn g2_add(a: PyG2, b: PyG2) -> PyResult<PyG2> {
    Ok(pyg2(&kernel::g2_add(&wg2(&a)?, &wg2(&b)?).map_err(err)?))
}

#[pyfunction]
fn g2_mul(p: PyG2, k: BigUint) -> PyResult<PyG2> {
    Ok(pyg2(&kernel::g2_mul(&wg2(&p)?, &w(&k)?).map_err(err)?))
}

/// EVM ecPairing semantics: `prod e(P_i, Q_i) == 1`.
#[pyfunction]
fn pairing_check(pairs: Vec<(PyG1, PyG2)>) -> PyResult<bool> {
    let mut v = Vec::with_capacity(pairs.len());
    for (p, q) in &pairs {
        v.push((wg1(p)?, wg2(q)?));
    }
    kernel::pairing::pairing_check(&v).map_err(err)
}

#[pyfunction]
fn keccak_scalar(words: Vec<BigUint>) -> PyResult<BigUint> {
    let ws: Vec<W256> = words.iter().map(w).collect::<PyResult<_>>()?;
    Ok(big(&kernel::keccak::keccak_scalar(&ws)))
}

#[pyfunction]
fn identity_scalar(canonical: &str) -> BigUint {
    big(&kernel::keccak::identity_scalar(canonical.as_bytes()))
}

#[pyfunction]
fn reduce_mod_order(v: BigUint) -> PyResult<BigUint> {
    Ok(big(&kernel::reduce_mod_order(&w(&v)?)))
}

#[pyfunction]
fn poseidon(inputs: Vec<BigUint>) -> PyResult<BigUint> {
    let ws: Vec<W256> = inputs.iter().map(w).collect::<PyResult<_>>()?;
    Ok(big(&kernel::poseidon::poseidon(&ws).map_err(err)?))
}

// ---------------------------------------------------------------------------
// ElGamal / PS
// ---------------------------------------------------------------------------

#[pyfunction]
fn elgamal_encrypt(m_point: PyG1, pk: PyG1, r: BigUint) -> PyResult<PyCt> {
    Ok(pyct(
        &kernel::elgamal::elgamal_encrypt(&wg1(&m_point)?, &wg1(&pk)?, &w(&r)?).map_err(err)?,
    ))
}

#[pyfunction]
fn elgamal_decrypt(e_ct: PyCt, sk: BigUint) -> PyResult<PyG1> {
    let ct = wct(&e_ct)?;
    Ok(pyg1(
        &kernel::elgamal::elgamal_decrypt(&ct.0, &ct.1, &w(&sk)?).map_err(err)?,
    ))
}

#[pyfunction]
fn ps_sign(sk_x: BigUint, sk_y: BigUint, m: BigUint, t: BigUint) -> PyResult<(PyG1, PyG1)> {
    let (s1, s2) =
        kernel::ps::ps_sign(&w(&sk_x)?, &w(&sk_y)?, &w(&m)?, &w(&t)?).map_err(err)?;
    Ok((pyg1(&s1), pyg1(&s2)))
}

#[pyfunction]
fn ps_verify(pk_x: PyG2, pk_y: PyG2, sigma_1: PyG1, sigma_2: PyG1, m: BigUint) -> PyResult<bool> {
    kernel::ps::ps_verify(
        &wg2(&pk_x)?,
        &wg2(&pk_y)?,
        &wg1(&sigma_1)?,
        &wg1(&sigma_2)?,
        &w(&m)?,
    )
    .map_err(err)
}

#[pyfunction]
fn ps_rerandomize(sigma_1: PyG1, sigma_2: PyG1, t: BigUint) -> PyResult<(PyG1, PyG1)> {
    let (s1, s2) =
        kernel::ps::ps_rerandomize(&wg1(&sigma_1)?, &wg1(&sigma_2)?, &w(&t)?).map_err(err)?;
    Ok((pyg1(&s1), pyg1(&s2)))
}

// ---------------------------------------------------------------------------
// Schnorr batch binding
// ---------------------------------------------------------------------------

/// Raw UNREDUCED keccak word (matches the Python reference).
#[pyfunction]
fn batch_commitment(cms: Vec<BigUint>) -> PyResult<BigUint> {
    let ws: Vec<W256> = cms.iter().map(w).collect::<PyResult<_>>()?;
    Ok(big(&kernel::schnorr::batch_commitment(&ws)))
}

#[pyfunction]
fn issuer_schnorr_sign(
    sk_iss: BigUint,
    h_batch: BigUint,
    issuer: BigUint,
    chainid: BigUint,
    k: BigUint,
) -> PyResult<(BigUint, BigUint, PyG1)> {
    let p = kernel::schnorr::issuer_schnorr_sign(
        &w(&sk_iss)?,
        &w(&h_batch)?,
        &w(&issuer)?,
        &w(&chainid)?,
        &w(&k)?,
    )
    .map_err(err)?;
    Ok((big(&p.e), big(&p.s), pyg1(&p.r)))
}

#[pyfunction]
fn issuer_schnorr_verify(
    pk_iss: PyG1,
    proof: (BigUint, BigUint, PyG1),
    h_batch: BigUint,
    issuer: BigUint,
    chainid: BigUint,
) -> PyResult<bool> {
    let p = kernel::schnorr::SchnorrProof {
        e: w(&proof.0)?,
        s: w(&proof.1)?,
        r: wg1(&proof.2)?,
    };
    kernel::schnorr::issuer_schnorr_verify(
        &wg1(&pk_iss)?,
        &p,
        &w(&h_batch)?,
        &w(&issuer)?,
        &w(&chainid)?,
    )
    .map_err(err)
}

// ---------------------------------------------------------------------------
// Registration NIZK
// ---------------------------------------------------------------------------

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn registration_prove(
    sigma_1: PyG1,
    sigma_2: PyG1,
    m: BigUint,
    r: BigUint,
    pk: PyG1,
    e_ct: PyCt,
    registrant: BigUint,
    m_tilde: BigUint,
    r_tilde: BigUint,
) -> PyResult<PyReg> {
    let p = kernel::nizk::registration_prove(
        &wg1(&sigma_1)?,
        &wg1(&sigma_2)?,
        &w(&m)?,
        &w(&r)?,
        &wg1(&pk)?,
        &wct(&e_ct)?,
        &w(&registrant)?,
        &w(&m_tilde)?,
        &w(&r_tilde)?,
    )
    .map_err(err)?;
    Ok((
        big(&p.e),
        big(&p.s_m),
        big(&p.s_r),
        pyg1(&p.a_ps),
        pyg1(&p.t_c),
        pyg1(&p.t_r),
    ))
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn registration_verify(
    sigma_1: PyG1,
    sigma_2: PyG1,
    e_ct: PyCt,
    pk: PyG1,
    issuer_x: PyG2,
    issuer_y: PyG2,
    proof: PyReg,
    registrant: BigUint,
) -> PyResult<bool> {
    let p = kernel::nizk::RegistrationProof {
        e: w(&proof.0)?,
        s_m: w(&proof.1)?,
        s_r: w(&proof.2)?,
        a_ps: wg1(&proof.3)?,
        t_c: wg1(&proof.4)?,
        t_r: wg1(&proof.5)?,
    };
    kernel::nizk::registration_verify(
        &wg1(&sigma_1)?,
        &wg1(&sigma_2)?,
        &wct(&e_ct)?,
        &wg1(&pk)?,
        &wg2(&issuer_x)?,
        &wg2(&issuer_y)?,
        &p,
        &w(&registrant)?,
    )
    .map_err(err)
}

// ---------------------------------------------------------------------------
// Chaum-Pedersen approve
// ---------------------------------------------------------------------------

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn chaum_pedersen_prove(
    e_alice: PyCt,
    e_bob: PyCt,
    pk_alice: PyG1,
    pk_bob: PyG1,
    sk_alice: BigUint,
    r_prime: BigUint,
    sender: BigUint,
    spender: BigUint,
    chainid: BigUint,
    k1: BigUint,
    k2: BigUint,
) -> PyResult<PyCp> {
    let p = kernel::chaum_pedersen::chaum_pedersen_prove(
        &wct(&e_alice)?,
        &wct(&e_bob)?,
        &wg1(&pk_alice)?,
        &wg1(&pk_bob)?,
        &w(&sk_alice)?,
        &w(&r_prime)?,
        &w(&sender)?,
        &w(&spender)?,
        &w(&chainid)?,
        &w(&k1)?,
        &w(&k2)?,
    )
    .map_err(err)?;
    Ok((
        big(&p.e),
        big(&p.s1),
        big(&p.s2),
        pyg1(&p.t1),
        pyg1(&p.t2),
        pyg1(&p.t3),
    ))
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn chaum_pedersen_verify(
    e_alice: PyCt,
    e_bob: PyCt,
    pk_alice: PyG1,
    pk_bob: PyG1,
    proof: PyCp,
    sender: BigUint,
    spender: BigUint,
    chainid: BigUint,
) -> PyResult<bool> {
    let p = kernel::chaum_pedersen::CpProof {
        e: w(&proof.0)?,
        s1: w(&proof.1)?,
        s2: w(&proof.2)?,
        t1: wg1(&proof.3)?,
        t2: wg1(&proof.4)?,
        t3: wg1(&proof.5)?,
    };
    kernel::chaum_pedersen::chaum_pedersen_verify(
        &wct(&e_alice)?,
        &wct(&e_bob)?,
        &wg1(&pk_alice)?,
        &wg1(&pk_bob)?,
        &p,
        &w(&sender)?,
        &w(&spender)?,
        &w(&chainid)?,
    )
    .map_err(err)
}

// ---------------------------------------------------------------------------
// Verifiable decryption
// ---------------------------------------------------------------------------

#[pyfunction]
fn verifiable_decrypt_prove(
    e_ct: PyCt,
    sk: BigUint,
    m_point: PyG1,
    account: BigUint,
    chainid: BigUint,
    t: BigUint,
) -> PyResult<PyVd> {
    let p = kernel::verifiable_decrypt::verifiable_decrypt_prove(
        &wct(&e_ct)?,
        &w(&sk)?,
        &wg1(&m_point)?,
        &w(&account)?,
        &w(&chainid)?,
        &w(&t)?,
    )
    .map_err(err)?;
    Ok((big(&p.e), big(&p.s), pyg1(&p.t1), pyg1(&p.t2)))
}

#[pyfunction]
fn verifiable_decrypt_verify(
    e_ct: PyCt,
    pk: PyG1,
    m_point: PyG1,
    proof: PyVd,
    account: BigUint,
    chainid: BigUint,
) -> PyResult<bool> {
    let p = kernel::verifiable_decrypt::VdProof {
        e: w(&proof.0)?,
        s: w(&proof.1)?,
        t1: wg1(&proof.2)?,
        t2: wg1(&proof.3)?,
    };
    kernel::verifiable_decrypt::verifiable_decrypt_verify(
        &wct(&e_ct)?,
        &wg1(&pk)?,
        &wg1(&m_point)?,
        &p,
        &w(&account)?,
        &w(&chainid)?,
    )
    .map_err(err)
}

// ---------------------------------------------------------------------------
// A2 issuer re-encryption binding
// ---------------------------------------------------------------------------

#[pyfunction]
fn h_point() -> PyG1 {
    pyg1(&kernel::issuer_reenc::h_point())
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn issuer_reenc_prove(
    sk_iss: BigUint,
    r_prime: BigUint,
    pk_rec: PyG1,
    e_reg: PyCt,
    e_iss: PyCt,
    issuer: BigUint,
    chainid: BigUint,
    beta: BigUint,
    gamma: BigUint,
    k_r: BigUint,
    k_b: BigUint,
    k_s: BigUint,
    k_g: BigUint,
) -> PyResult<PyIr> {
    let p = kernel::issuer_reenc::issuer_reenc_prove(
        &w(&sk_iss)?,
        &w(&r_prime)?,
        &wg1(&pk_rec)?,
        &wct(&e_reg)?,
        &wct(&e_iss)?,
        &w(&issuer)?,
        &w(&chainid)?,
        &w(&beta)?,
        &w(&gamma)?,
        &w(&k_r)?,
        &w(&k_b)?,
        &w(&k_s)?,
        &w(&k_g)?,
    )
    .map_err(err)?;
    Ok((
        (
            big(&p.e),
            big(&p.s_r),
            big(&p.s_b),
            big(&p.s_s),
            big(&p.s_g),
        ),
        (
            pyg1(&p.a1),
            pyg1(&p.a2),
            pyg1(&p.a3),
            pyg1(&p.a4),
            pyg1(&p.a5),
            pyg1(&p.q),
            pyg1(&p.u),
            pyg1(&p.t),
        ),
    ))
}

#[pyfunction]
fn issuer_reenc_verify(
    pk_iss: PyG1,
    e_reg: PyCt,
    e_iss: PyCt,
    proof: PyIr,
    issuer: BigUint,
    chainid: BigUint,
) -> PyResult<bool> {
    let (s, pts) = &proof;
    let p = kernel::issuer_reenc::IssuerReencProof {
        e: w(&s.0)?,
        s_r: w(&s.1)?,
        s_b: w(&s.2)?,
        s_s: w(&s.3)?,
        s_g: w(&s.4)?,
        a1: wg1(&pts.0)?,
        a2: wg1(&pts.1)?,
        a3: wg1(&pts.2)?,
        a4: wg1(&pts.3)?,
        a5: wg1(&pts.4)?,
        q: wg1(&pts.5)?,
        u: wg1(&pts.6)?,
        t: wg1(&pts.7)?,
    };
    kernel::issuer_reenc::issuer_reenc_verify(
        &wg1(&pk_iss)?,
        &wct(&e_reg)?,
        &wct(&e_iss)?,
        &p,
        &w(&issuer)?,
        &w(&chainid)?,
    )
    .map_err(err)
}

// ---------------------------------------------------------------------------
// Deposit coupling / B1 depositor binding
// ---------------------------------------------------------------------------

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn deposit_couple_prove(
    m_rec: BigUint,
    sk_dep: BigUint,
    e_dep: PyCt,
    e_iss: PyCt,
    account: BigUint,
    chainid: BigUint,
    b: BigUint,
    k_m: BigUint,
    k_s: BigUint,
    k_b: BigUint,
) -> PyResult<PyDc> {
    let p = kernel::unilateral_a2::deposit_couple_prove(
        &w(&m_rec)?,
        &w(&sk_dep)?,
        &wct(&e_dep)?,
        &wct(&e_iss)?,
        &w(&account)?,
        &w(&chainid)?,
        &w(&b)?,
        &w(&k_m)?,
        &w(&k_s)?,
        &w(&k_b)?,
    )
    .map_err(err)?;
    Ok((
        big(&p.e),
        big(&p.s_m),
        big(&p.s_s),
        big(&p.s_b),
        pyg1(&p.a2),
        pyg1(&p.a3),
        pyg1(&p.a4),
        pyg1(&p.p_i),
    ))
}

#[pyfunction]
fn deposit_couple_verify(
    pk_dep: PyG1,
    e_dep: PyCt,
    e_iss: PyCt,
    proof: PyDc,
    account: BigUint,
    chainid: BigUint,
) -> PyResult<bool> {
    let p = kernel::unilateral_a2::DepositCouplingProof {
        e: w(&proof.0)?,
        s_m: w(&proof.1)?,
        s_s: w(&proof.2)?,
        s_b: w(&proof.3)?,
        a2: wg1(&proof.4)?,
        a3: wg1(&proof.5)?,
        a4: wg1(&proof.6)?,
        p_i: wg1(&proof.7)?,
    };
    kernel::unilateral_a2::deposit_couple_verify(
        &wg1(&pk_dep)?,
        &wct(&e_dep)?,
        &wct(&e_iss)?,
        &p,
        &w(&account)?,
        &w(&chainid)?,
    )
    .map_err(err)
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn b1_bind_prove(
    m_dep: BigUint,
    sk_dep: BigUint,
    e_dep: PyCt,
    pk_iss: PyG1,
    account: BigUint,
    chainid: BigUint,
    r: BigUint,
    b: BigUint,
    k_m: BigUint,
    k_s: BigUint,
    k_r: BigUint,
    k_b: BigUint,
) -> PyResult<(PyDb, PyCt)> {
    let (p, e_f) = kernel::b1_binding::b1_bind_prove(
        &w(&m_dep)?,
        &w(&sk_dep)?,
        &wct(&e_dep)?,
        &wg1(&pk_iss)?,
        &w(&account)?,
        &w(&chainid)?,
        &w(&r)?,
        &w(&b)?,
        &w(&k_m)?,
        &w(&k_s)?,
        &w(&k_r)?,
        &w(&k_b)?,
    )
    .map_err(err)?;
    Ok((
        (
            big(&p.e),
            big(&p.s_m),
            big(&p.s_s),
            big(&p.s_r),
            big(&p.s_b),
            pyg1(&p.a2),
            pyg1(&p.a4),
            pyg1(&p.b1),
            pyg1(&p.b2),
            pyg1(&p.a_p),
            pyg1(&p.p_dep),
        ),
        pyct(&e_f),
    ))
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn b1_bind_verify(
    pk_dep: PyG1,
    e_dep: PyCt,
    pk_iss: PyG1,
    e_dep_for_iss: PyCt,
    proof: PyDb,
    account: BigUint,
    chainid: BigUint,
) -> PyResult<bool> {
    let p = kernel::b1_binding::DepositorBindingProof {
        e: w(&proof.0)?,
        s_m: w(&proof.1)?,
        s_s: w(&proof.2)?,
        s_r: w(&proof.3)?,
        s_b: w(&proof.4)?,
        a2: wg1(&proof.5)?,
        a4: wg1(&proof.6)?,
        b1: wg1(&proof.7)?,
        b2: wg1(&proof.8)?,
        a_p: wg1(&proof.9)?,
        p_dep: wg1(&proof.10)?,
    };
    kernel::b1_binding::b1_bind_verify(
        &wg1(&pk_dep)?,
        &wct(&e_dep)?,
        &wg1(&pk_iss)?,
        &wct(&e_dep_for_iss)?,
        &p,
        &w(&account)?,
        &w(&chainid)?,
    )
    .map_err(err)
}

// ---------------------------------------------------------------------------
// Notes family
// ---------------------------------------------------------------------------

#[pyfunction]
fn note_commitment(
    flavor: u64,
    v: BigUint,
    rho: BigUint,
    id_hash: BigUint,
    predicate: BigUint,
) -> PyResult<BigUint> {
    Ok(big(
        &kernel::notes::note_commitment(flavor, &w(&v)?, &w(&rho)?, &w(&id_hash)?, &w(&predicate)?)
            .map_err(err)?,
    ))
}

#[pyfunction]
fn nullifier_b(rho: BigUint, id_hash: BigUint) -> PyResult<BigUint> {
    Ok(big(&kernel::notes::nullifier_b(&w(&rho)?, &w(&id_hash)?).map_err(err)?))
}

#[pyfunction]
fn nullifier_a(rho: BigUint, id_hash: BigUint) -> PyResult<BigUint> {
    Ok(big(&kernel::notes::nullifier_a(&w(&rho)?, &w(&id_hash)?).map_err(err)?))
}

#[pyfunction]
fn id_hash_b1(m_issuer: BigUint, sigma_r: PyG1, sigma_s: BigUint) -> PyResult<BigUint> {
    Ok(big(
        &kernel::notes::id_hash_b1(&w(&m_issuer)?, &wg1(&sigma_r)?, &w(&sigma_s)?).map_err(err)?,
    ))
}

#[pyfunction]
fn id_hash_a1(
    e_note: PyCt,
    m_issuer: BigUint,
    sigma_r: PyG1,
    sigma_s: BigUint,
) -> PyResult<BigUint> {
    Ok(big(
        &kernel::notes::id_hash_a1(&wct(&e_note)?, &w(&m_issuer)?, &wg1(&sigma_r)?, &w(&sigma_s)?)
            .map_err(err)?,
    ))
}

#[pyfunction]
fn id_hash_a2(e_note: PyCt, e_iss: PyCt) -> PyResult<BigUint> {
    Ok(big(&kernel::notes::id_hash_a2(&wct(&e_note)?, &wct(&e_iss)?).map_err(err)?))
}

#[pyfunction]
fn identity_leaf(m_point: PyG1) -> PyResult<BigUint> {
    Ok(big(&kernel::notes::identity_leaf(&wg1(&m_point)?).map_err(err)?))
}

// ---------------------------------------------------------------------------
// Module
// ---------------------------------------------------------------------------

#[pymodule]
fn buck_identity(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Constants (plain Python ints / tuples)
    m.add("ORDER", big(&kernel::order()))?;
    m.add("F_R", big(&kernel::order()))?;
    m.add("FIELD_MODULUS", big(&kernel::field_modulus()))?;
    m.add("G1", pyg1(&kernel::g1_generator()))?;
    m.add("G2", pyg2(&kernel::g2_generator()))?;
    m.add("H_POINT", pyg1(&kernel::issuer_reenc::h_point()))?;
    m.add("NULLIFIER_TAG_B", kernel::notes::NULLIFIER_TAG_B)?;
    m.add("NULLIFIER_TAG_A", kernel::notes::NULLIFIER_TAG_A)?;
    m.add("FLAVOR_A1", kernel::notes::FLAVOR_A1)?;
    m.add("FLAVOR_A2", kernel::notes::FLAVOR_A2)?;
    m.add("FLAVOR_B1", kernel::notes::FLAVOR_B1)?;

    m.add_function(wrap_pyfunction!(g1_add, m)?)?;
    m.add_function(wrap_pyfunction!(g1_mul, m)?)?;
    m.add_function(wrap_pyfunction!(g1_neg, m)?)?;
    m.add_function(wrap_pyfunction!(g2_add, m)?)?;
    m.add_function(wrap_pyfunction!(g2_mul, m)?)?;
    m.add_function(wrap_pyfunction!(pairing_check, m)?)?;
    m.add_function(wrap_pyfunction!(keccak_scalar, m)?)?;
    m.add_function(wrap_pyfunction!(identity_scalar, m)?)?;
    m.add_function(wrap_pyfunction!(reduce_mod_order, m)?)?;
    m.add_function(wrap_pyfunction!(poseidon, m)?)?;
    m.add_function(wrap_pyfunction!(elgamal_encrypt, m)?)?;
    m.add_function(wrap_pyfunction!(elgamal_decrypt, m)?)?;
    m.add_function(wrap_pyfunction!(ps_sign, m)?)?;
    m.add_function(wrap_pyfunction!(ps_verify, m)?)?;
    m.add_function(wrap_pyfunction!(ps_rerandomize, m)?)?;
    m.add_function(wrap_pyfunction!(batch_commitment, m)?)?;
    m.add_function(wrap_pyfunction!(issuer_schnorr_sign, m)?)?;
    m.add_function(wrap_pyfunction!(issuer_schnorr_verify, m)?)?;
    m.add_function(wrap_pyfunction!(registration_prove, m)?)?;
    m.add_function(wrap_pyfunction!(registration_verify, m)?)?;
    m.add_function(wrap_pyfunction!(chaum_pedersen_prove, m)?)?;
    m.add_function(wrap_pyfunction!(chaum_pedersen_verify, m)?)?;
    m.add_function(wrap_pyfunction!(verifiable_decrypt_prove, m)?)?;
    m.add_function(wrap_pyfunction!(verifiable_decrypt_verify, m)?)?;
    m.add_function(wrap_pyfunction!(h_point, m)?)?;
    m.add_function(wrap_pyfunction!(issuer_reenc_prove, m)?)?;
    m.add_function(wrap_pyfunction!(issuer_reenc_verify, m)?)?;
    m.add_function(wrap_pyfunction!(deposit_couple_prove, m)?)?;
    m.add_function(wrap_pyfunction!(deposit_couple_verify, m)?)?;
    m.add_function(wrap_pyfunction!(b1_bind_prove, m)?)?;
    m.add_function(wrap_pyfunction!(b1_bind_verify, m)?)?;
    m.add_function(wrap_pyfunction!(note_commitment, m)?)?;
    m.add_function(wrap_pyfunction!(nullifier_b, m)?)?;
    m.add_function(wrap_pyfunction!(nullifier_a, m)?)?;
    m.add_function(wrap_pyfunction!(id_hash_b1, m)?)?;
    m.add_function(wrap_pyfunction!(id_hash_a1, m)?)?;
    m.add_function(wrap_pyfunction!(id_hash_a2, m)?)?;
    m.add_function(wrap_pyfunction!(identity_leaf, m)?)?;
    Ok(())
}
