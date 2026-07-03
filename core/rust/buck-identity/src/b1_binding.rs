//! B1 depositor binding -- mirrors the sigma protocol half of
//! `alberta_buck/wallet/b1_binding.py` (`b1_bind_prove/verify`).
//!
//! At spend the depositor publishes `E_dep_for_iss = (r*G, M_dep +
//! r*pk_iss)` and proves it encrypts the registered Identity bound to the
//! payout account -- the mirror image of the A2 deposit coupling, with
//! `P_dep = M_dep + b*H` published for the membership tie.

use ark_bn254::{G1Affine, G1Projective};
use ark_ec::{AffineRepr, CurveGroup};

use crate::issuer_reenc::h_affine;
use crate::{fr_mod, g1_from_w, w_from_fr, w_from_g1, G1w, IdError, Result, Transcript, W256};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DepositorBindingProof {
    pub e: W256,
    pub s_m: W256,
    pub s_s: W256,
    pub s_r: W256,
    pub s_b: W256,
    pub a2: G1w,
    pub a4: G1w,
    pub b1: G1w,
    pub b2: G1w,
    pub a_p: G1w,
    pub p_dep: G1w,
}

#[allow(clippy::too_many_arguments)]
fn transcript(
    pk_dep: &G1Affine,
    e_dep: &(G1Affine, G1Affine),
    pk_iss: &G1Affine,
    e_dep_for_iss: &(G1Affine, G1Affine),
    a2: &G1Affine,
    a4: &G1Affine,
    b1: &G1Affine,
    b2: &G1Affine,
    a_p: &G1Affine,
    p_dep: &G1Affine,
    account: &W256,
    chainid: &W256,
) -> Transcript {
    let mut t = Transcript::new();
    t.p(pk_dep)
        .p(&e_dep.0)
        .p(&e_dep.1)
        .p(pk_iss)
        .p(&e_dep_for_iss.0)
        .p(&e_dep_for_iss.1)
        .p(a2)
        .p(a4)
        .p(b1)
        .p(b2)
        .p(a_p)
        .p(p_dep)
        .w(account)
        .w(chainid);
    t
}

/// Build `E_dep_for_iss` and prove it, with the caller's `r` (encryption
/// randomness), blind `b`, and nonces `k_m`, `k_s`, `k_r`, `k_b`.
/// Returns `(proof, E_dep_for_iss)`.
#[allow(clippy::too_many_arguments)]
pub fn b1_bind_prove(
    m_dep: &W256,
    sk_dep: &W256,
    e_dep: &(G1w, G1w),
    pk_iss: &G1w,
    account: &W256,
    chainid: &W256,
    r: &W256,
    b: &W256,
    k_m: &W256,
    k_s: &W256,
    k_r: &W256,
    k_b: &W256,
) -> Result<(DepositorBindingProof, (G1w, G1w))> {
    let m = fr_mod(m_dep);
    let sk = fr_mod(sk_dep);
    let r_d = g1_from_w(&e_dep.0)?;
    let c_d = g1_from_w(&e_dep.1)?;
    let pk_iss = g1_from_w(pk_iss)?;
    let g = G1Affine::generator();
    let h = h_affine();

    let pk_dep = (g * sk).into_affine();
    let m_dep_pt: G1Projective = g * m;

    // Sanity: the payout account must be bound to identity m_dep.
    if G1Projective::from(c_d) != m_dep_pt + r_d * sk {
        return Err(IdError("E_dep does not decrypt to m_dep*G under sk_dep"));
    }

    let r = fr_mod(r);
    let e_f = (
        (g * r).into_affine(),                    // r*G
        (m_dep_pt + pk_iss * r).into_affine(),    // M_dep + r*pk_iss
    );

    let b = fr_mod(b);
    let p_dep = (m_dep_pt + h * b).into_affine(); // M_dep + b*H

    let k_m = fr_mod(k_m);
    let k_s = fr_mod(k_s);
    let k_r = fr_mod(k_r);
    let k_b = fr_mod(k_b);
    let a4 = (g * k_s).into_affine(); //                     k_s*G
    let a2 = (g * k_m + r_d * k_s).into_affine(); //         k_m*G + k_s*R_d
    let b1 = (g * k_r).into_affine(); //                     k_r*G
    let b2 = (g * k_m + pk_iss * k_r).into_affine(); //      k_m*G + k_r*pk_iss
    let a_p = (g * k_m + h * k_b).into_affine(); //          k_m*G + k_b*H

    let e = transcript(
        &pk_dep,
        &(r_d, c_d),
        &pk_iss,
        &(e_f.0, e_f.1),
        &a2,
        &a4,
        &b1,
        &b2,
        &a_p,
        &p_dep,
        account,
        chainid,
    )
    .e();

    Ok((
        DepositorBindingProof {
            e: w_from_fr(&e),
            s_m: w_from_fr(&(k_m + e * m)),
            s_s: w_from_fr(&(k_s + e * sk)),
            s_r: w_from_fr(&(k_r + e * r)),
            s_b: w_from_fr(&(k_b + e * b)),
            a2: w_from_g1(&a2),
            a4: w_from_g1(&a4),
            b1: w_from_g1(&b1),
            b2: w_from_g1(&b2),
            a_p: w_from_g1(&a_p),
            p_dep: w_from_g1(&p_dep),
        },
        (w_from_g1(&e_f.0), w_from_g1(&e_f.1)),
    ))
}

/// Verify the five relations plus Fiat-Shamir.
#[allow(clippy::too_many_arguments)]
pub fn b1_bind_verify(
    pk_dep: &G1w,
    e_dep: &(G1w, G1w),
    pk_iss: &G1w,
    e_dep_for_iss: &(G1w, G1w),
    proof: &DepositorBindingProof,
    account: &W256,
    chainid: &W256,
) -> Result<bool> {
    let pk_dep = g1_from_w(pk_dep)?;
    let r_d = g1_from_w(&e_dep.0)?;
    let c_d = g1_from_w(&e_dep.1)?;
    let pk_iss = g1_from_w(pk_iss)?;
    let r_f = g1_from_w(&e_dep_for_iss.0)?;
    let c_f = g1_from_w(&e_dep_for_iss.1)?;
    let a2 = g1_from_w(&proof.a2)?;
    let a4 = g1_from_w(&proof.a4)?;
    let b1 = g1_from_w(&proof.b1)?;
    let b2 = g1_from_w(&proof.b2)?;
    let a_p = g1_from_w(&proof.a_p)?;
    let p_dep = g1_from_w(&proof.p_dep)?;
    let e = fr_mod(&proof.e);
    let s_m = fr_mod(&proof.s_m);
    let s_s = fr_mod(&proof.s_s);
    let s_r = fr_mod(&proof.s_r);
    let s_b = fr_mod(&proof.s_b);
    let g = G1Affine::generator();
    let h = h_affine();

    // E4: s_s*G == A4 + e*pk_dep
    if g * s_s != pk_dep * e + a4 {
        return Ok(false);
    }
    // E2: s_m*G + s_s*R_d == A2 + e*C_d
    if g * s_m + r_d * s_s != c_d * e + a2 {
        return Ok(false);
    }
    // F1: s_r*G == B1 + e*R_f
    if g * s_r != r_f * e + b1 {
        return Ok(false);
    }
    // F2: s_m*G + s_r*pk_iss == B2 + e*C_f
    if g * s_m + pk_iss * s_r != c_f * e + b2 {
        return Ok(false);
    }
    // P: s_m*G + s_b*H == A_p + e*P_dep
    if g * s_m + h * s_b != p_dep * e + a_p {
        return Ok(false);
    }
    // Fiat-Shamir
    let e_check = transcript(
        &pk_dep,
        &(r_d, c_d),
        &pk_iss,
        &(r_f, c_f),
        &a2,
        &a4,
        &b1,
        &b2,
        &a_p,
        &p_dep,
        account,
        chainid,
    )
    .e();
    Ok(w_from_fr(&e_check) == proof.e)
}
