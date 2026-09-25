//! Registration NIZK (A') -- mirrors `alberta_buck/wallet/nizk.py`.
//!
//! Binds a hiding PS presentation `(A, B)` to an ElGamal ciphertext and
//! proves the registrant holds `sk` for `pk`:
//!   (a') `e(B, g_2) == e(A, X) * e(m*A + b*G, Y)` -- `(A, B)` presents a
//!        valid credential on `m`,
//!   (b)  `E = (r*G, m*G + r*pk)` encrypts the same `m`,
//!   (k)  `pk = sk*G`.
//! One commitment `C1 = m_tilde*A + b_tilde*G` covers both credential
//! exponents, so no proof field reveals `m*A` on its own.  Fiat-Shamir binds
//! chainid, the registry contract address, and domain
//! `AlbertaBuck/FiatShamir/IdentityRegistry/Register/v2`.
//! Verified on-chain by `IdentityRegistry.register`.

use ark_bn254::{G1Affine, G2Affine};
use ark_ec::{AffineRepr, CurveGroup};

use crate::pairing::product_is_one;
use crate::{
    fr_mod, g1_from_w, g2_from_w, w_from_fr, w_from_g1, w_lt_order, G1w, G2w, Result, Transcript,
    W256,
};

/// Full keccak word of `FS_REGISTER` -- not reduced mod ORDER.
/// Mirrors `alberta_buck.wallet.nizk.REGISTER_DOMAIN` and
/// `IdentityRegistry.REGISTER_DOMAIN`.
fn register_domain() -> W256 {
    crate::domains::word(crate::domains::FS_REGISTER)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RegistrationProof {
    pub e: W256,
    pub s_m: W256,
    pub s_b: W256,
    pub s_r: W256,
    pub s_sk: W256,
    pub c1: G1w,
    pub t_c: G1w,
    pub t_r: G1w,
    pub t_key: G1w,
}

#[allow(clippy::too_many_arguments)]
fn transcript(
    a: &G1Affine,
    b: &G1Affine,
    e_r: &G1Affine,
    e_c: &G1Affine,
    pk: &G1Affine,
    c1: &G1Affine,
    t_c: &G1Affine,
    t_r: &G1Affine,
    t_key: &G1Affine,
    registrant: &W256,
    chainid: &W256,
    registry: &W256,
) -> Transcript {
    let domain = register_domain();
    let mut t = Transcript::new();
    t.p(a)
        .p(b)
        .p(e_r)
        .p(e_c)
        .p(pk)
        .p(c1)
        .p(t_c)
        .p(t_r)
        .p(t_key)
        .w(registrant)
        .w(chainid)
        .w(registry)
        .w(&domain);
    t
}

/// Prove for the presentation `(A, B)` with blinding `blind` (= `b`) and the
/// caller's commitment nonces `m_tilde`, `b_tilde`, `r_tilde`, `sk_tilde`
/// (the Python draw order).
#[allow(clippy::too_many_arguments)]
pub fn registration_prove(
    a: &G1w,
    b: &G1w,
    blind: &W256,
    m: &W256,
    r: &W256,
    pk: &G1w,
    e_ct: &(G1w, G1w),
    registrant: &W256,
    sk: &W256,
    chainid: &W256,
    registry: &W256,
    m_tilde: &W256,
    b_tilde: &W256,
    r_tilde: &W256,
    sk_tilde: &W256,
) -> Result<RegistrationProof> {
    let pa = g1_from_w(a)?;
    let pb = g1_from_w(b)?;
    let pk = g1_from_w(pk)?;
    let er = g1_from_w(&e_ct.0)?;
    let ec = g1_from_w(&e_ct.1)?;
    let m_tilde = fr_mod(m_tilde);
    let b_tilde = fr_mod(b_tilde);
    let r_tilde = fr_mod(r_tilde);
    let sk_tilde = fr_mod(sk_tilde);

    let c1 = (pa * m_tilde + G1Affine::generator() * b_tilde).into_affine();
    let t_c = (G1Affine::generator() * m_tilde + pk * r_tilde).into_affine();
    let t_r = (G1Affine::generator() * r_tilde).into_affine();
    let t_key = (G1Affine::generator() * sk_tilde).into_affine();

    let e = transcript(
        &pa, &pb, &er, &ec, &pk, &c1, &t_c, &t_r, &t_key,
        registrant, chainid, registry,
    )
    .e();
    let s_m = m_tilde + e * fr_mod(m);
    let s_b = b_tilde + e * fr_mod(blind);
    let s_r = r_tilde + e * fr_mod(r);
    let s_sk = sk_tilde + e * fr_mod(sk);

    Ok(RegistrationProof {
        e: w_from_fr(&e),
        s_m: w_from_fr(&s_m),
        s_b: w_from_fr(&s_b),
        s_r: w_from_fr(&s_r),
        s_sk: w_from_fr(&s_sk),
        c1: w_from_g1(&c1),
        t_c: w_from_g1(&t_c),
        t_r: w_from_g1(&t_r),
        t_key: w_from_g1(&t_key),
    })
}

/// Mirror of the Solidity verifier.
#[allow(clippy::too_many_arguments)]
pub fn registration_verify(
    a: &G1w,
    b: &G1w,
    e_ct: &(G1w, G1w),
    pk: &G1w,
    issuer_x: &G2w,
    issuer_y: &G2w,
    proof: &RegistrationProof,
    registrant: &W256,
    chainid: &W256,
    registry: &W256,
) -> Result<bool> {
    if !w_lt_order(&proof.e)
        || !w_lt_order(&proof.s_m)
        || !w_lt_order(&proof.s_b)
        || !w_lt_order(&proof.s_r)
        || !w_lt_order(&proof.s_sk)
    {
        return Ok(false);
    }

    let pa = g1_from_w(a)?;
    let pb = g1_from_w(b)?;
    let pk = g1_from_w(pk)?;
    let er = g1_from_w(&e_ct.0)?;
    let ec = g1_from_w(&e_ct.1)?;
    let c1 = g1_from_w(&proof.c1)?;
    let t_c = g1_from_w(&proof.t_c)?;
    let t_r = g1_from_w(&proof.t_r)?;
    let t_key = g1_from_w(&proof.t_key)?;
    let x = g2_from_w(issuer_x)?;
    let y = g2_from_w(issuer_y)?;

    // A = O makes the credential term vanish and (a') holds for every m.
    if pa.is_zero() || pb.is_zero() || pk.is_zero() || er.is_zero() {
        return Ok(false);
    }

    let e_check = transcript(
        &pa, &pb, &er, &ec, &pk, &c1, &t_c, &t_r, &t_key,
        registrant, chainid, registry,
    )
    .e();
    if w_from_fr(&e_check) != proof.e {
        return Ok(false);
    }

    let e = fr_mod(&proof.e);
    let s_m = fr_mod(&proof.s_m);
    let s_b = fr_mod(&proof.s_b);
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

    // (a') presentation pairing product (three pairs):
    //   e(s_m*A + s_b*G - C1, Y) * e(e*A, X) * e(-e*B, g2) == 1
    let lhs = (pa * s_m + G1Affine::generator() * s_b - c1.into_group()).into_affine();
    Ok(product_is_one(&[
        (lhs, y),
        ((pa * e).into_affine(), x),
        ((-(pb * e)).into_affine(), G2Affine::generator()),
    ]))
}
