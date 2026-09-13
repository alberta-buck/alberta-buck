//! Registration NIZK -- mirrors `alberta_buck/wallet/nizk.py`.
//!
//! Binds a rerandomized PS signature to an ElGamal ciphertext and proves
//! the registrant holds `sk` for `pk`: (a) `sigma'` is a valid PS signature
//! on `m`, (b) `E = (r*G, m*G + r*pk)` encrypts the same `m`, (k) `pk = sk*G`.
//! Fiat-Shamir binds chainid and domain `AlbertaBuck:Register:v2`, not the
//! registry contract address (fixture proofs replay onto many deployments).
//! Verified on-chain by `IdentityRegistry.register`.

use ark_bn254::{G1Affine, G2Affine};
use ark_ec::{AffineRepr, CurveGroup};

use crate::keccak::keccak_raw;
use crate::pairing::product_is_one;
use crate::{
    fr_mod, g1_from_w, g2_from_w, w_from_fr, w_from_g1, w_lt_order, G1w, G2w, Result, Transcript,
    W256,
};

/// Full keccak word of `AlbertaBuck:Register:v2` -- not reduced mod ORDER.
/// Mirrors `alberta_buck.wallet.nizk.REGISTER_DOMAIN` and
/// `IdentityRegistry.REGISTER_DOMAIN`.
fn register_domain() -> W256 {
    keccak_raw(b"AlbertaBuck:Register:v2")
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RegistrationProof {
    pub e: W256,
    pub s_m: W256,
    pub s_r: W256,
    pub s_sk: W256,
    pub a_ps: G1w,
    pub t_c: G1w,
    pub t_r: G1w,
    pub t_key: G1w,
}

#[allow(clippy::too_many_arguments)]
fn transcript(
    sigma_1: &G1Affine,
    sigma_2: &G1Affine,
    e_r: &G1Affine,
    e_c: &G1Affine,
    pk: &G1Affine,
    a_ps: &G1Affine,
    t_c: &G1Affine,
    t_r: &G1Affine,
    t_key: &G1Affine,
    registrant: &W256,
    chainid: &W256,
) -> Transcript {
    let domain = register_domain();
    let mut t = Transcript::new();
    t.p(sigma_1)
        .p(sigma_2)
        .p(e_r)
        .p(e_c)
        .p(pk)
        .p(a_ps)
        .p(t_c)
        .p(t_r)
        .p(t_key)
        .w(registrant)
        .w(chainid)
        .w(&domain);
    t
}

/// Prove with the caller's commitment nonces `m_tilde`, `r_tilde`, `sk_tilde`.
#[allow(clippy::too_many_arguments)]
pub fn registration_prove(
    sigma_1: &G1w,
    sigma_2: &G1w,
    m: &W256,
    r: &W256,
    pk: &G1w,
    e_ct: &(G1w, G1w),
    registrant: &W256,
    sk: &W256,
    chainid: &W256,
    m_tilde: &W256,
    r_tilde: &W256,
    sk_tilde: &W256,
) -> Result<RegistrationProof> {
    let s1 = g1_from_w(sigma_1)?;
    let s2 = g1_from_w(sigma_2)?;
    let pk = g1_from_w(pk)?;
    let er = g1_from_w(&e_ct.0)?;
    let ec = g1_from_w(&e_ct.1)?;
    let m_tilde = fr_mod(m_tilde);
    let r_tilde = fr_mod(r_tilde);
    let sk_tilde = fr_mod(sk_tilde);

    let a_ps = (s1 * m_tilde).into_affine();
    let t_c = (G1Affine::generator() * m_tilde + pk * r_tilde).into_affine();
    let t_r = (G1Affine::generator() * r_tilde).into_affine();
    let t_key = (G1Affine::generator() * sk_tilde).into_affine();

    let e = transcript(
        &s1, &s2, &er, &ec, &pk, &a_ps, &t_c, &t_r, &t_key, registrant, chainid,
    )
    .e();
    let s_m = m_tilde + e * fr_mod(m);
    let s_r = r_tilde + e * fr_mod(r);
    let s_sk = sk_tilde + e * fr_mod(sk);

    Ok(RegistrationProof {
        e: w_from_fr(&e),
        s_m: w_from_fr(&s_m),
        s_r: w_from_fr(&s_r),
        s_sk: w_from_fr(&s_sk),
        a_ps: w_from_g1(&a_ps),
        t_c: w_from_g1(&t_c),
        t_r: w_from_g1(&t_r),
        t_key: w_from_g1(&t_key),
    })
}

/// Mirror of the Solidity verifier.
#[allow(clippy::too_many_arguments)]
pub fn registration_verify(
    sigma_1: &G1w,
    sigma_2: &G1w,
    e_ct: &(G1w, G1w),
    pk: &G1w,
    issuer_x: &G2w,
    issuer_y: &G2w,
    proof: &RegistrationProof,
    registrant: &W256,
    chainid: &W256,
) -> Result<bool> {
    if !w_lt_order(&proof.e)
        || !w_lt_order(&proof.s_m)
        || !w_lt_order(&proof.s_r)
        || !w_lt_order(&proof.s_sk)
    {
        return Ok(false);
    }

    let s1 = g1_from_w(sigma_1)?;
    let s2 = g1_from_w(sigma_2)?;
    let pk = g1_from_w(pk)?;
    let er = g1_from_w(&e_ct.0)?;
    let ec = g1_from_w(&e_ct.1)?;
    let a_ps = g1_from_w(&proof.a_ps)?;
    let t_c = g1_from_w(&proof.t_c)?;
    let t_r = g1_from_w(&proof.t_r)?;
    let t_key = g1_from_w(&proof.t_key)?;
    let x = g2_from_w(issuer_x)?;
    let y = g2_from_w(issuer_y)?;

    if s1.is_zero() || pk.is_zero() || er.is_zero() {
        return Ok(false);
    }

    let e_check = transcript(
        &s1, &s2, &er, &ec, &pk, &a_ps, &t_c, &t_r, &t_key, registrant, chainid,
    )
    .e();
    if w_from_fr(&e_check) != proof.e {
        return Ok(false);
    }

    let e = fr_mod(&proof.e);
    let s_m = fr_mod(&proof.s_m);
    let s_r = fr_mod(&proof.s_r);
    let s_sk = fr_mod(&proof.s_sk);

    // (b) ElGamal C consistency: s_m*G + s_r*pk == e*C + T_C
    if G1Affine::generator() * s_m + pk * s_r != ec * e + t_c {
        return Ok(false);
    }

    // (c) ElGamal R consistency: s_r*G == e*R + T_R
    if G1Affine::generator() * s_r != er * e + t_r {
        return Ok(false);
    }

    // (k) Account-key ownership: s_sk*G == T_key + e*pk
    if G1Affine::generator() * s_sk != t_key + pk * e {
        return Ok(false);
    }

    // (a) PS pairing product:
    //   e(s_m*sigma_1, Y) * e(-A_ps, Y) * e(e*sigma_1, X) * e(-e*sigma_2, g2) == 1
    Ok(product_is_one(&[
        ((s1 * s_m).into_affine(), y),
        (-a_ps, y),
        ((s1 * e).into_affine(), x),
        ((-(s2 * e)).into_affine(), G2Affine::generator()),
    ]))
}
