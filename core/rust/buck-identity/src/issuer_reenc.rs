//! A2 issuer re-encryption binding -- mirrors
//! `alberta_buck/wallet/issuer_reenc.py`.
//!
//! Recipient-blinded proof that a private issuer's `E_iss-for-rec`
//! re-encrypts the issuer's REGISTERED Identity under the recipient's key,
//! revealing neither `pk_rec` nor `M_iss`.  Five-relation Okamoto sigma
//! (witnesses `r'`, `beta`, `sk_iss`, `gamma`) over the published blinds
//! `Q = pk_rec + beta*H`, `U = r'*H`, `T = r'*pk_rec + gamma*G`.

use std::sync::OnceLock;

use ark_bn254::{Fr, G1Affine, G1Projective};
use ark_ec::{AffineRepr, CurveGroup};
use ark_ff::PrimeField;

use crate::keccak::keccak_raw;
use crate::{fr_mod, g1_from_w, w_from_fr, w_from_g1, G1w, IdError, Result, Transcript, W256};

/// Second generator H -- nothing-up-my-sleeve:
/// `H = (keccak256("AlbertaBuck:IssuerReenc:H") mod ORDER) * G1`.
pub(crate) fn h_affine() -> G1Affine {
    static H: OnceLock<G1Affine> = OnceLock::new();
    *H.get_or_init(|| {
        let s = Fr::from_be_bytes_mod_order(&keccak_raw(b"AlbertaBuck:IssuerReenc:H"));
        (G1Affine::generator() * s).into_affine()
    })
}

/// H as a word pair (for bindings and callers).
pub fn h_point() -> G1w {
    w_from_g1(&h_affine())
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IssuerReencProof {
    pub e: W256,
    pub s_r: W256,
    pub s_b: W256,
    pub s_s: W256,
    pub s_g: W256,
    pub a1: G1w,
    pub a2: G1w,
    pub a3: G1w,
    pub a4: G1w,
    pub a5: G1w,
    pub q: G1w,
    pub u: G1w,
    pub t: G1w,
}

#[allow(clippy::too_many_arguments)]
fn transcript(
    pts: &[&G1Affine; 13],
    issuer: &W256,
    chainid: &W256,
) -> Transcript {
    let mut t = Transcript::new();
    for p in pts {
        t.p(p);
    }
    t.w(issuer)
        .w(chainid)
        .w(&crate::domains::word(crate::domains::FS_ISSUER_REENC));
    t
}

/// Prove the binding.  All blinds and nonces (`beta`, `gamma`, `k_r`,
/// `k_b`, `k_s`, `k_g`) are the caller's.  The witness-consistency checks
/// mirror the Python asserts so misuse fails loudly.
#[allow(clippy::too_many_arguments)]
pub fn issuer_reenc_prove(
    sk_iss: &W256,
    r_prime: &W256,
    pk_rec: &G1w,
    e_reg: &(G1w, G1w),
    e_iss: &(G1w, G1w),
    issuer: &W256,
    chainid: &W256,
    beta: &W256,
    gamma: &W256,
    k_r: &W256,
    k_b: &W256,
    k_s: &W256,
    k_g: &W256,
) -> Result<IssuerReencProof> {
    let sk = fr_mod(sk_iss);
    let rp = fr_mod(r_prime);
    let pk_rec = g1_from_w(pk_rec)?;
    let r_reg = g1_from_w(&e_reg.0)?;
    let c_reg = g1_from_w(&e_reg.1)?;
    let r_i = g1_from_w(&e_iss.0)?;
    let c_i = g1_from_w(&e_iss.1)?;
    let g = G1Affine::generator();
    let h = h_affine();

    let pk_iss = (g * sk).into_affine();

    // Sanity: the leaf must actually re-encrypt the issuer's registered M.
    let m_iss: G1Projective = G1Projective::from(c_reg) - r_reg * sk;
    if G1Projective::from(r_i) != g * rp {
        return Err(IdError("E_iss.R != r'*G"));
    }
    if G1Projective::from(c_i) != m_iss + pk_rec * rp {
        return Err(IdError("E_iss.C != M_iss + r'*pk_rec"));
    }

    let beta = fr_mod(beta);
    let gamma = fr_mod(gamma);

    // Published values.
    let q = (G1Projective::from(pk_rec) + h * beta).into_affine(); // pk_rec + beta*H
    let u = (h * rp).into_affine(); //                                r'*H
    let t = (pk_rec * rp + g * gamma).into_affine(); //               r'*pk_rec + gamma*G

    // Commitments.
    let k_r = fr_mod(k_r);
    let k_b = fr_mod(k_b);
    let k_s = fr_mod(k_s);
    let k_g = fr_mod(k_g);
    let a1 = (g * k_r).into_affine(); //                              k_r*G
    let a2 = (h * k_r).into_affine(); //                              k_r*H
    let a3 = (q * k_r - u * k_b + g * k_g).into_affine(); //          k_r*Q - k_b*U + k_g*G
    let a4 = (g * k_s).into_affine(); //                              k_s*G
    let a5 = (r_reg * k_s + g * k_g).into_affine(); //                k_s*R_reg + k_g*G

    let e = transcript(
        &[&pk_iss, &r_reg, &c_reg, &r_i, &c_i, &q, &u, &t, &a1, &a2, &a3, &a4, &a5],
        issuer,
        chainid,
    )
    .e();

    Ok(IssuerReencProof {
        e: w_from_fr(&e),
        s_r: w_from_fr(&(k_r + e * rp)),
        s_b: w_from_fr(&(k_b + e * beta)),
        s_s: w_from_fr(&(k_s + e * sk)),
        s_g: w_from_fr(&(k_g + e * gamma)),
        a1: w_from_g1(&a1),
        a2: w_from_g1(&a2),
        a3: w_from_g1(&a3),
        a4: w_from_g1(&a4),
        a5: w_from_g1(&a5),
        q: w_from_g1(&q),
        u: w_from_g1(&u),
        t: w_from_g1(&t),
    })
}

/// Verify the binding's five relations plus Fiat-Shamir.
pub fn issuer_reenc_verify(
    pk_iss: &G1w,
    e_reg: &(G1w, G1w),
    e_iss: &(G1w, G1w),
    proof: &IssuerReencProof,
    issuer: &W256,
    chainid: &W256,
) -> Result<bool> {
    let pk_iss = g1_from_w(pk_iss)?;
    let r_reg = g1_from_w(&e_reg.0)?;
    let c_reg = g1_from_w(&e_reg.1)?;
    let r_i = g1_from_w(&e_iss.0)?;
    let c_i = g1_from_w(&e_iss.1)?;
    let q = g1_from_w(&proof.q)?;
    let u = g1_from_w(&proof.u)?;
    let t = g1_from_w(&proof.t)?;
    let a1 = g1_from_w(&proof.a1)?;
    let a2 = g1_from_w(&proof.a2)?;
    let a3 = g1_from_w(&proof.a3)?;
    let a4 = g1_from_w(&proof.a4)?;
    let a5 = g1_from_w(&proof.a5)?;
    let e = fr_mod(&proof.e);
    let s_r = fr_mod(&proof.s_r);
    let s_b = fr_mod(&proof.s_b);
    let s_s = fr_mod(&proof.s_s);
    let s_g = fr_mod(&proof.s_g);
    let g = G1Affine::generator();
    let h = h_affine();

    // L1: s_r*G == A1 + e*R_i
    if g * s_r != r_i * e + a1 {
        return Ok(false);
    }
    // L2: s_r*H == A2 + e*U
    if h * s_r != u * e + a2 {
        return Ok(false);
    }
    // L3: s_r*Q - s_b*U + s_g*G == A3 + e*T
    if q * s_r - u * s_b + g * s_g != t * e + a3 {
        return Ok(false);
    }
    // L4: s_s*G == A4 + e*pk_iss
    if g * s_s != pk_iss * e + a4 {
        return Ok(false);
    }
    // L5: s_s*R_reg + s_g*G == A5 + e*(C_reg + T - C_i)
    let y: G1Projective = G1Projective::from(c_reg) + t - c_i;
    if r_reg * s_s + g * s_g != y * e + a5 {
        return Ok(false);
    }

    // Fiat-Shamir
    let e_check = transcript(
        &[&pk_iss, &r_reg, &c_reg, &r_i, &c_i, &q, &u, &t, &a1, &a2, &a3, &a4, &a5],
        issuer,
        chainid,
    )
    .e();
    Ok(w_from_fr(&e_check) == proof.e)
}
