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
type PyReg = (BigUint, BigUint, BigUint, BigUint, BigUint, PyG1, PyG1, PyG1, PyG1);
type PyCp = (BigUint, BigUint, BigUint, PyG1, PyG1, PyG1);
type PyVd = (BigUint, BigUint, PyG1, PyG1);
// PyO3 tuples cap at 12 elements; the 13-field issuer-reenc proof nests as
// ((e, s_r, s_b, s_s, s_g), (A1, A2, A3, A4, A5, Q, U, T)).
#[allow(clippy::type_complexity)]
type PyIr = (
    (BigUint, BigUint, BigUint, BigUint, BigUint),
    (PyG1, PyG1, PyG1, PyG1, PyG1, PyG1, PyG1, PyG1),
);
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

/// `(A, B) = (a*sigma_1, a*sigma_2 + b*Y1)`: the hiding presentation.
#[pyfunction]
fn ps_present(sigma_1: PyG1, sigma_2: PyG1, y1: PyG1, a: BigUint, b: BigUint) -> PyResult<(PyG1, PyG1)> {
    let (pa, pb) = kernel::ps::ps_present(&wg1(&sigma_1)?, &wg1(&sigma_2)?, &wg1(&y1)?, &w(&a)?, &w(&b)?)
        .map_err(err)?;
    Ok((pyg1(&pa), pyg1(&pb)))
}

/// `e(Y1, g_2) == e(G, Y)`.
#[pyfunction]
fn ps_key_consistent(pk_y: PyG2, y1: PyG1) -> PyResult<bool> {
    kernel::ps::ps_key_consistent(&wg2(&pk_y)?, &wg1(&y1)?).map_err(err)
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
// Registration NIZK (A')
// ---------------------------------------------------------------------------

/// Prove for the presentation `(A, B)` with blinding `blind`; nonces in the
/// Python draw order `m_tilde, b_tilde, r_tilde, sk_tilde`.  Returns the
/// nine-field proof `(e, s_m, s_b, s_r, s_sk, C1, T_C, T_R, T_key)`.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn registration_prove(
    a: PyG1,
    b: PyG1,
    blind: BigUint,
    m: BigUint,
    r: BigUint,
    pk: PyG1,
    e_ct: PyCt,
    registrant: BigUint,
    sk: BigUint,
    chainid: BigUint,
    registry: BigUint,
    m_tilde: BigUint,
    b_tilde: BigUint,
    r_tilde: BigUint,
    sk_tilde: BigUint,
) -> PyResult<PyReg> {
    let p = kernel::nizk::registration_prove(
        &wg1(&a)?,
        &wg1(&b)?,
        &w(&blind)?,
        &w(&m)?,
        &w(&r)?,
        &wg1(&pk)?,
        &wct(&e_ct)?,
        &w(&registrant)?,
        &w(&sk)?,
        &w(&chainid)?,
        &w(&registry)?,
        &w(&m_tilde)?,
        &w(&b_tilde)?,
        &w(&r_tilde)?,
        &w(&sk_tilde)?,
    )
    .map_err(err)?;
    Ok((
        big(&p.e),
        big(&p.s_m),
        big(&p.s_b),
        big(&p.s_r),
        big(&p.s_sk),
        pyg1(&p.c1),
        pyg1(&p.t_c),
        pyg1(&p.t_r),
        pyg1(&p.t_key),
    ))
}

/// The A' verifier.  Exported as `registration_verify_v3`; the Python wallet
/// dispatches to the kernel only when this name exists, so a stale build
/// falls back to the pure-Python path instead of verifying the wrong relation.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn registration_verify_v3(
    a: PyG1,
    b: PyG1,
    e_ct: PyCt,
    pk: PyG1,
    issuer_x: PyG2,
    issuer_y: PyG2,
    proof: PyReg,
    registrant: BigUint,
    chainid: BigUint,
    registry: BigUint,
) -> PyResult<bool> {
    let p = kernel::nizk::RegistrationProof {
        e: w(&proof.0)?,
        s_m: w(&proof.1)?,
        s_b: w(&proof.2)?,
        s_r: w(&proof.3)?,
        s_sk: w(&proof.4)?,
        c1: wg1(&proof.5)?,
        t_c: wg1(&proof.6)?,
        t_r: wg1(&proof.7)?,
        t_key: wg1(&proof.8)?,
    };
    kernel::nizk::registration_verify(
        &wg1(&a)?,
        &wg1(&b)?,
        &wct(&e_ct)?,
        &wg1(&pk)?,
        &wg2(&issuer_x)?,
        &wg2(&issuer_y)?,
        &p,
        &w(&registrant)?,
        &w(&chainid)?,
        &w(&registry)?,
    )
    .map_err(err)
}

/// Same verifier under the historical name (the relation is A').
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn registration_verify(
    a: PyG1,
    b: PyG1,
    e_ct: PyCt,
    pk: PyG1,
    issuer_x: PyG2,
    issuer_y: PyG2,
    proof: PyReg,
    registrant: BigUint,
    chainid: BigUint,
    registry: BigUint,
) -> PyResult<bool> {
    registration_verify_v3(a, b, e_ct, pk, issuer_x, issuer_y, proof, registrant, chainid, registry)
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
    registry: BigUint,
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
        &w(&registry)?,
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
    registry: BigUint,
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
        &w(&registry)?,
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

/// The same DLEQ, its transcript bound to (account, chainid, registry) under
/// its own tag: an Identity opening to one registry, never a receipt.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn identity_opening_prove(
    e_ct: PyCt,
    sk: BigUint,
    m_point: PyG1,
    account: BigUint,
    chainid: BigUint,
    registry: BigUint,
    t: BigUint,
) -> PyResult<PyVd> {
    let p = kernel::verifiable_decrypt::identity_opening_prove(
        &wct(&e_ct)?,
        &w(&sk)?,
        &wg1(&m_point)?,
        &w(&account)?,
        &w(&chainid)?,
        &w(&registry)?,
        &w(&t)?,
    )
    .map_err(err)?;
    Ok((big(&p.e), big(&p.s), pyg1(&p.t1), pyg1(&p.t2)))
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn identity_opening_verify(
    e_ct: PyCt,
    pk: PyG1,
    m_point: PyG1,
    proof: PyVd,
    account: BigUint,
    chainid: BigUint,
    registry: BigUint,
) -> PyResult<bool> {
    let p = kernel::verifiable_decrypt::VdProof {
        e: w(&proof.0)?,
        s: w(&proof.1)?,
        t1: wg1(&proof.2)?,
        t2: wg1(&proof.3)?,
    };
    kernel::verifiable_decrypt::identity_opening_verify(
        &wct(&e_ct)?,
        &wg1(&pk)?,
        &wg1(&m_point)?,
        &p,
        &w(&account)?,
        &w(&chainid)?,
        &w(&registry)?,
    )
    .map_err(err)
}

// ---------------------------------------------------------------------------
// Holder-derived salts
// ---------------------------------------------------------------------------

#[pyfunction]
fn tree_tag(tree_id: &str) -> PyResult<BigUint> {
    Ok(big(&kernel::salt::tree_tag(tree_id).map_err(err)?))
}

#[pyfunction]
#[pyo3(signature = (holder_secret, tree_id, association_counter=0))]
fn derive_salt(holder_secret: BigUint, tree_id: &str, association_counter: u64) -> PyResult<BigUint> {
    Ok(big(&kernel::salt::derive_salt(&w(&holder_secret)?, tree_id, association_counter).map_err(err)?))
}

// ---------------------------------------------------------------------------
// A2 issuer re-encryption binding
// ---------------------------------------------------------------------------

/// The Pedersen generator: hashed to the curve, so its discrete log is
/// unknown.  Every blind in the protocol sits on it; see the kernel's `nums`.
#[pyfunction]
fn h_pedersen() -> PyG1 {
    pyg1(&kernel::nums::h_pedersen())
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
// B1 depositor binding
// ---------------------------------------------------------------------------

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
fn nullifier(rho: BigUint, id_hash: BigUint) -> PyResult<BigUint> {
    Ok(big(&kernel::notes::nullifier(&w(&rho)?, &w(&id_hash)?).map_err(err)?))
}

#[pyfunction]
fn id_hash_b1(m_issuer: BigUint) -> PyResult<BigUint> {
    Ok(big(&kernel::notes::id_hash_b1(&w(&m_issuer)?).map_err(err)?))
}

#[pyfunction]
fn id_hash_a1(e_note: PyCt, m_issuer: BigUint) -> PyResult<BigUint> {
    Ok(big(&kernel::notes::id_hash_a1(&wct(&e_note)?, &w(&m_issuer)?).map_err(err)?))
}

/// `id_hash_a2(eNote, eIss, T)` -- `T` is the mint binding's blinded point.
#[pyfunction]
fn id_hash_a2(e_note: PyCt, e_iss: PyCt, t: PyG1) -> PyResult<BigUint> {
    Ok(big(
        &kernel::notes::id_hash_a2(&wct(&e_note)?, &wct(&e_iss)?, &wg1(&t)?).map_err(err)?,
    ))
}

#[pyfunction]
fn identity_leaf(m_point: PyG1) -> PyResult<BigUint> {
    Ok(big(&kernel::notes::identity_leaf(&wg1(&m_point)?).map_err(err)?))
}

/// `identity_leaf_salted(M, salt)` -- the hiding leaf of a private subtree.
#[pyfunction]
fn identity_leaf_salted(m_point: PyG1, salt: BigUint) -> PyResult<BigUint> {
    Ok(big(
        &kernel::notes::identity_leaf_salted(&wg1(&m_point)?, &w(&salt)?).map_err(err)?,
    ))
}

/// `receiving_leaf(m_rec, k_recv, salt)` -- the hiding leaf binding an
/// Identity to the receiving key its Notes are addressed to.  Over scalars.
#[pyfunction]
fn receiving_leaf(
    m_rec: BigUint,
    k_recv: BigUint,
    salt: BigUint,
) -> PyResult<BigUint> {
    Ok(big(&kernel::notes::receiving_leaf(
        &w(&m_rec)?,
        &w(&k_recv)?,
        &w(&salt)?,
    )
    .map_err(err)?))
}

// ---------------------------------------------------------------------------
// buck_wallet: canonical dialect, AB-RCPT/2 envelope, receipt build /
// verify and the unilateral flows.  Structured inputs cross as ONE JSON
// text of named args (the vector-fixture shapes); receipt cores cross as
// their canonical text -- see buck-wallet's `args` module.
// ---------------------------------------------------------------------------

use pyo3::types::PyBytes;

fn jerr(e: kernel::IdError) -> PyErr {
    PyValueError::new_err(e.0)
}

fn parse_args(args_json: &str) -> PyResult<serde_json::Value> {
    serde_json::from_str(args_json)
        .map_err(|e| PyValueError::new_err(format!("args: invalid JSON: {e}")))
}

#[pyfunction]
fn canonical_json(text: &str) -> PyResult<String> {
    wallet::canonical::canonical_json(text).map_err(jerr)
}

#[pyfunction]
fn canonical_identity_data(fields_json: &str) -> PyResult<String> {
    wallet::canonical::canonical_identity_data(fields_json).map_err(jerr)
}

#[pyfunction]
#[pyo3(signature = (canonical, prefix_len = 12))]
fn receipt_id(canonical: &[u8], prefix_len: usize) -> String {
    wallet::envelope::receipt_id(canonical, prefix_len)
}

#[pyfunction]
#[pyo3(signature = (canonical, width = 64))]
fn envelope_text(canonical: &[u8], width: usize) -> String {
    wallet::envelope::envelope_text(canonical, width)
}

#[pyfunction]
fn parse_envelope(py: Python<'_>, text: &str) -> PyResult<Py<PyBytes>> {
    let bytes = wallet::envelope::parse_envelope(text).map_err(jerr)?;
    Ok(PyBytes::new(py, &bytes).into())
}

/// Tier-1 verify of a canonical receipt text; returns the RcptResult as
/// a JSON text `{"ok", "reason", "identity_M", "value"}`.
#[pyfunction]
fn verify_receipt(core_text: &str) -> PyResult<String> {
    wallet::args::verify_receipt_args(core_text).map_err(jerr)
}

/// Build any receipt kind from named JSON args; returns the canonical
/// receipt text.
#[pyfunction]
fn build_receipt(args_json: &str) -> PyResult<String> {
    wallet::args::build_receipt_args(&parse_args(args_json)?).map_err(jerr)
}

#[pyfunction]
fn mint_unilateral_a2(args_json: &str) -> PyResult<String> {
    wallet::args::mint_unilateral_a2_args(&parse_args(args_json)?).map_err(jerr)
}

#[pyfunction]
fn make_receipt_a2(args_json: &str) -> PyResult<String> {
    wallet::args::make_receipt_a2_args(&parse_args(args_json)?).map_err(jerr)
}

#[pyfunction]
fn verify_receipt_a2(args_json: &str) -> PyResult<String> {
    wallet::args::verify_receipt_a2_args(&parse_args(args_json)?).map_err(jerr)
}

#[pyfunction]
fn mint_unilateral_a1(args_json: &str) -> PyResult<String> {
    wallet::args::mint_unilateral_a1_args(&parse_args(args_json)?).map_err(jerr)
}

#[pyfunction]
fn make_receipt_a1(args_json: &str) -> PyResult<String> {
    wallet::args::make_receipt_a1_args(&parse_args(args_json)?).map_err(jerr)
}

#[pyfunction]
fn verify_receipt_a1(args_json: &str) -> PyResult<String> {
    wallet::args::verify_receipt_a1_args(&parse_args(args_json)?).map_err(jerr)
}

#[pyfunction]
fn issue_credential(args_json: &str) -> PyResult<String> {
    wallet::args::issue_credential_args(&parse_args(args_json)?).map_err(jerr)
}

// ---- Notes: receiving key, delivery, mailbox binding, fold witnesses ----

#[pyfunction]
fn receiving_key(args_json: &str) -> PyResult<String> {
    wallet::args::receiving_key_args(&parse_args(args_json)?).map_err(jerr)
}

#[pyfunction]
fn wrap_mask(args_json: &str) -> PyResult<String> {
    wallet::args::wrap_mask_args(&parse_args(args_json)?).map_err(jerr)
}

#[pyfunction]
fn deliver_a1(args_json: &str) -> PyResult<String> {
    wallet::args::deliver_a1_args(&parse_args(args_json)?).map_err(jerr)
}

#[pyfunction]
fn deliver_a2(args_json: &str) -> PyResult<String> {
    wallet::args::deliver_a2_args(&parse_args(args_json)?).map_err(jerr)
}

#[pyfunction]
fn open_a1(args_json: &str) -> PyResult<String> {
    wallet::args::open_a1_args(&parse_args(args_json)?).map_err(jerr)
}

#[pyfunction]
fn open_a2(args_json: &str) -> PyResult<String> {
    wallet::args::open_a2_args(&parse_args(args_json)?).map_err(jerr)
}

#[pyfunction]
fn prove_receiving_binding(args_json: &str) -> PyResult<String> {
    wallet::args::prove_receiving_binding_args(&parse_args(args_json)?).map_err(jerr)
}

#[pyfunction]
fn verify_receiving_binding(args_json: &str) -> PyResult<String> {
    wallet::args::verify_receiving_binding_args(&parse_args(args_json)?).map_err(jerr)
}

#[pyfunction]
fn deposit_fold_a1_witness(args_json: &str) -> PyResult<String> {
    wallet::args::deposit_fold_a1_witness_args(&parse_args(args_json)?).map_err(jerr)
}

#[pyfunction]
fn deposit_fold_a2_witness(args_json: &str) -> PyResult<String> {
    wallet::args::deposit_fold_a2_witness_args(&parse_args(args_json)?).map_err(jerr)
}

#[pymodule]
fn buck_wallet(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("ENVELOPE_HEADER", wallet::envelope::ENVELOPE_HEADER)?;
    m.add("ENVELOPE_FOOTER", wallet::envelope::ENVELOPE_FOOTER)?;
    m.add_function(wrap_pyfunction!(canonical_json, m)?)?;
    m.add_function(wrap_pyfunction!(canonical_identity_data, m)?)?;
    m.add_function(wrap_pyfunction!(receipt_id, m)?)?;
    m.add_function(wrap_pyfunction!(envelope_text, m)?)?;
    m.add_function(wrap_pyfunction!(parse_envelope, m)?)?;
    m.add_function(wrap_pyfunction!(verify_receipt, m)?)?;
    m.add_function(wrap_pyfunction!(build_receipt, m)?)?;
    m.add_function(wrap_pyfunction!(mint_unilateral_a2, m)?)?;
    m.add_function(wrap_pyfunction!(receiving_key, m)?)?;
    m.add_function(wrap_pyfunction!(wrap_mask, m)?)?;
    m.add_function(wrap_pyfunction!(deliver_a1, m)?)?;
    m.add_function(wrap_pyfunction!(deliver_a2, m)?)?;
    m.add_function(wrap_pyfunction!(open_a1, m)?)?;
    m.add_function(wrap_pyfunction!(open_a2, m)?)?;
    m.add_function(wrap_pyfunction!(prove_receiving_binding, m)?)?;
    m.add_function(wrap_pyfunction!(verify_receiving_binding, m)?)?;
    m.add_function(wrap_pyfunction!(deposit_fold_a1_witness, m)?)?;
    m.add_function(wrap_pyfunction!(deposit_fold_a2_witness, m)?)?;
    m.add_function(wrap_pyfunction!(make_receipt_a2, m)?)?;
    m.add_function(wrap_pyfunction!(verify_receipt_a2, m)?)?;
    m.add_function(wrap_pyfunction!(mint_unilateral_a1, m)?)?;
    m.add_function(wrap_pyfunction!(make_receipt_a1, m)?)?;
    m.add_function(wrap_pyfunction!(verify_receipt_a1, m)?)?;
    m.add_function(wrap_pyfunction!(issue_credential, m)?)?;
    Ok(())
}

// ---------------------------------------------------------------------------
// buck_registry: certificates cross as their wire bytes (vector-pinned),
// the registry Schnorr as tuples in the wallet's word convention.
// ---------------------------------------------------------------------------

fn msg32(msg_hash: &[u8]) -> PyResult<[u8; 32]> {
    msg_hash
        .try_into()
        .map_err(|_| PyValueError::new_err("msg_hash must be exactly 32 bytes"))
}

#[pyfunction]
fn registry_schnorr_sign(
    sk: BigUint,
    msg_hash: &[u8],
    registry_id: &str,
    chainid: BigUint,
    k: BigUint,
) -> PyResult<(BigUint, BigUint, PyG1)> {
    let p = registry::certificate::registry_schnorr_sign(
        &w(&sk)?,
        &msg32(msg_hash)?,
        registry_id,
        &w(&chainid)?,
        &w(&k)?,
    )
    .map_err(jerr)?;
    Ok((big(&p.e), big(&p.s), pyg1(&p.r)))
}

#[pyfunction]
fn registry_schnorr_verify(
    pk: PyG1,
    e: BigUint,
    s: BigUint,
    r_point: PyG1,
    msg_hash: &[u8],
    registry_id: &str,
    chainid: BigUint,
) -> PyResult<bool> {
    let proof = registry::certificate::RegistrySchnorrProof {
        e: w(&e)?,
        s: w(&s)?,
        r: wg1(&r_point)?,
    };
    registry::certificate::registry_schnorr_verify(
        &wg1(&pk)?,
        &proof,
        &msg32(msg_hash)?,
        registry_id,
        &w(&chainid)?,
    )
    .map_err(jerr)
}

/// Create + sign a certificate; returns the SignedCertificate WIRE bytes
/// (`e,s,R,pk` header + certificate payload) -- the format
/// `alberta_buck.registry.certificate.SignedCertificate.serialize` pins.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn registry_sign_certificate(
    py: Python<'_>,
    registry_sk: BigUint,
    registry_id: &str,
    canonical_identity: &str,
    serial: u64,
    issued_at: i64,
    expires_at: i64,
    chainid: BigUint,
    k: BigUint,
) -> PyResult<Py<PyBytes>> {
    let signed = registry::certificate::registry_sign_certificate(
        &w(&registry_sk)?,
        registry_id,
        canonical_identity,
        serial,
        issued_at,
        expires_at,
        &w(&chainid)?,
        &w(&k)?,
    )
    .map_err(jerr)?;
    Ok(PyBytes::new(py, &signed.serialize()).into())
}

/// Verify a SignedCertificate from its wire bytes (signature + the
/// M/canonical-identity consistency).
#[pyfunction]
fn registry_verify_certificate(signed_wire: &[u8], chainid: BigUint) -> PyResult<bool> {
    let signed =
        registry::certificate::SignedCertificate::deserialize(signed_wire).map_err(jerr)?;
    registry::certificate::registry_verify_certificate(&signed, &w(&chainid)?).map_err(jerr)
}

/// ElGamal-seal a signed certificate (wire bytes) for a client; returns
/// the SealedCertificate envelope bytes.
#[pyfunction]
fn seal_certificate(
    py: Python<'_>,
    signed_wire: &[u8],
    client_pk: PyG1,
    r: BigUint,
) -> PyResult<Py<PyBytes>> {
    let signed =
        registry::certificate::SignedCertificate::deserialize(signed_wire).map_err(jerr)?;
    let sealed = registry::certificate::seal_certificate(&signed, &wg1(&client_pk)?, &w(&r)?)
        .map_err(jerr)?;
    Ok(PyBytes::new(py, &sealed.envelope()).into())
}

/// Unseal a SealedCertificate envelope; returns the SignedCertificate
/// wire bytes (raises if the envelope was not sealed for this client).
#[pyfunction]
fn unseal_certificate(py: Python<'_>, envelope: &[u8], client_sk: BigUint) -> PyResult<Py<PyBytes>> {
    let sealed = registry::certificate::SealedCertificate::from_envelope(envelope).map_err(jerr)?;
    let signed = registry::certificate::unseal_certificate(&sealed, &w(&client_sk)?).map_err(jerr)?;
    Ok(PyBytes::new(py, &signed.serialize()).into())
}

/// `(standing, face_band, dep_types, max_dep_rate, max_premium_rate,
/// expires_at, scopes)`, the `InsurerEnvelope` fields in order.
type PyEnv = (bool, u8, Vec<u8>, u32, u32, f64, Vec<BigUint>);

fn face_units(v: &BigUint) -> PyResult<u128> {
    u128::try_from(v).map_err(|_| PyValueError::new_err("face_units out of range (>= 2^128)"))
}

/// BuckCredit's issuance gate: "" if the credit is admitted, otherwise its
/// revert reason.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn check_issuance(
    env: PyEnv,
    scope: BigUint,
    face: BigUint,
    dep_type: u8,
    dep_rate: u32,
    premium_rate: u32,
    now: f64,
) -> PyResult<String> {
    let scopes = env.6.iter().map(w).collect::<PyResult<_>>()?;
    let e = registry::regulator::InsurerEnvelope::new(
        env.0,
        env.1,
        env.2.into_iter().collect(),
        env.3,
        env.4,
        env.5,
        scopes,
    )
    .map_err(jerr)?;
    Ok(
        match registry::regulator::check_issuance(&e, &w(&scope)?, face_units(&face)?, dep_type, dep_rate, premium_rate, now) {
            Ok(()) => String::new(),
            Err(r) => r.0.to_string(),
        },
    )
}

#[pyfunction]
fn band_for_face(face: BigUint) -> PyResult<u8> {
    Ok(registry::regulator::band_for_face(face_units(&face)?))
}

#[pyfunction]
fn scope_id(name: &str) -> PyResult<BigUint> {
    Ok(big(&registry::regulator::scope_id(name).map_err(jerr)?))
}

#[pyfunction]
fn subtree_key(name: &str) -> PyResult<BigUint> {
    Ok(big(&registry::regulator::subtree_key(name).map_err(jerr)?))
}

#[pymodule]
fn buck_registry(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(check_issuance, m)?)?;
    m.add_function(wrap_pyfunction!(band_for_face, m)?)?;
    m.add_function(wrap_pyfunction!(scope_id, m)?)?;
    m.add_function(wrap_pyfunction!(subtree_key, m)?)?;
    m.add_function(wrap_pyfunction!(registry_schnorr_sign, m)?)?;
    m.add_function(wrap_pyfunction!(registry_schnorr_verify, m)?)?;
    m.add_function(wrap_pyfunction!(registry_sign_certificate, m)?)?;
    m.add_function(wrap_pyfunction!(registry_verify_certificate, m)?)?;
    m.add_function(wrap_pyfunction!(seal_certificate, m)?)?;
    m.add_function(wrap_pyfunction!(unseal_certificate, m)?)?;
    Ok(())
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
    m.add("H_PEDERSEN", pyg1(&kernel::nums::h_pedersen()))?;
    m.add_function(wrap_pyfunction!(h_pedersen, m)?)?;
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
    m.add_function(wrap_pyfunction!(ps_present, m)?)?;
    m.add_function(wrap_pyfunction!(ps_key_consistent, m)?)?;
    m.add_function(wrap_pyfunction!(batch_commitment, m)?)?;
    m.add_function(wrap_pyfunction!(issuer_schnorr_sign, m)?)?;
    m.add_function(wrap_pyfunction!(issuer_schnorr_verify, m)?)?;
    m.add_function(wrap_pyfunction!(registration_prove, m)?)?;
    m.add_function(wrap_pyfunction!(registration_verify, m)?)?;
    m.add_function(wrap_pyfunction!(registration_verify_v3, m)?)?;
    m.add_function(wrap_pyfunction!(chaum_pedersen_prove, m)?)?;
    m.add_function(wrap_pyfunction!(chaum_pedersen_verify, m)?)?;
    m.add_function(wrap_pyfunction!(verifiable_decrypt_prove, m)?)?;
    m.add_function(wrap_pyfunction!(verifiable_decrypt_verify, m)?)?;
    m.add_function(wrap_pyfunction!(identity_opening_prove, m)?)?;
    m.add_function(wrap_pyfunction!(identity_opening_verify, m)?)?;
    m.add_function(wrap_pyfunction!(tree_tag, m)?)?;
    m.add_function(wrap_pyfunction!(derive_salt, m)?)?;
    m.add_function(wrap_pyfunction!(issuer_reenc_prove, m)?)?;
    m.add_function(wrap_pyfunction!(issuer_reenc_verify, m)?)?;
    m.add_function(wrap_pyfunction!(b1_bind_prove, m)?)?;
    m.add_function(wrap_pyfunction!(b1_bind_verify, m)?)?;
    m.add_function(wrap_pyfunction!(note_commitment, m)?)?;
    m.add_function(wrap_pyfunction!(nullifier, m)?)?;
    m.add_function(wrap_pyfunction!(id_hash_b1, m)?)?;
    m.add_function(wrap_pyfunction!(id_hash_a1, m)?)?;
    m.add_function(wrap_pyfunction!(id_hash_a2, m)?)?;
    m.add_function(wrap_pyfunction!(identity_leaf, m)?)?;
    m.add_function(wrap_pyfunction!(identity_leaf_salted, m)?)?;
    m.add_function(wrap_pyfunction!(receiving_leaf, m)?)?;
    Ok(())
}
