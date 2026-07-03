//! A2 deposit coupling -- mirrors the sigma protocol half of
//! `alberta_buck/wallet/unilateral_a2.py` (`deposit_couple_prove/verify`).
//!
//! The depositor proves knowledge of `(m_rec, sk_dep, b)` tying the deposit
//! account's registered credential to the identity that decrypts the note's
//! `eIss`, all identities hidden behind `P_I = M_I + b*H`.
//!
//! (The mint / receipt orchestration and the `IdentityTree` container stay
//! language-side; their crypto calls land here.)

use ark_bn254::{G1Affine, G1Projective};
use ark_ec::{AffineRepr, CurveGroup};

use crate::issuer_reenc::h_affine;
use crate::{fr_mod, g1_from_w, w_from_fr, w_from_g1, G1w, IdError, Result, Transcript, W256};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DepositCouplingProof {
    pub e: W256,
    pub s_m: W256,
    pub s_s: W256,
    pub s_b: W256,
    pub a2: G1w,
    pub a3: G1w,
    pub a4: G1w,
    pub p_i: G1w,
}

#[allow(clippy::too_many_arguments)]
fn transcript(
    pk_dep: &G1Affine,
    e_dep: &(G1Affine, G1Affine),
    e_iss: &(G1Affine, G1Affine),
    p_i: &G1Affine,
    a2: &G1Affine,
    a3: &G1Affine,
    a4: &G1Affine,
    account: &W256,
    chainid: &W256,
) -> Transcript {
    let mut t = Transcript::new();
    t.p(pk_dep)
        .p(&e_dep.0)
        .p(&e_dep.1)
        .p(&e_iss.0)
        .p(&e_iss.1)
        .p(p_i)
        .p(a2)
        .p(a3)
        .p(a4)
        .w(account)
        .w(chainid);
    t
}

/// Prove deposit eligibility with the caller's blind `b` and nonces
/// `k_m`, `k_s`, `k_b`.  The witness-consistency check mirrors the Python
/// assert.
#[allow(clippy::too_many_arguments)]
pub fn deposit_couple_prove(
    m_rec: &W256,
    sk_dep: &W256,
    e_dep: &(G1w, G1w),
    e_iss: &(G1w, G1w),
    account: &W256,
    chainid: &W256,
    b: &W256,
    k_m: &W256,
    k_s: &W256,
    k_b: &W256,
) -> Result<DepositCouplingProof> {
    let m = fr_mod(m_rec);
    let sk = fr_mod(sk_dep);
    let r_d = g1_from_w(&e_dep.0)?;
    let c_d = g1_from_w(&e_dep.1)?;
    let r_e = g1_from_w(&e_iss.0)?;
    let c_e = g1_from_w(&e_iss.1)?;
    let g = G1Affine::generator();
    let h = h_affine();

    let pk_dep = (g * sk).into_affine();
    let m_rec_pt: G1Projective = g * m;
    let m_i: G1Projective = G1Projective::from(c_e) - r_e * m; // decrypt eIss under m_rec

    // Sanity: the deposit account must be bound to identity m_rec.
    if G1Projective::from(c_d) != m_rec_pt + r_d * sk {
        return Err(IdError("E_dep does not decrypt to m_rec*G under sk_dep"));
    }

    let b = fr_mod(b);
    let p_i = (m_i + h * b).into_affine(); // commit/hide the issuer identity

    let k_m = fr_mod(k_m);
    let k_s = fr_mod(k_s);
    let k_b = fr_mod(k_b);
    let a4 = (g * k_s).into_affine(); //                    k_s*G
    let a2 = (g * k_m + r_d * k_s).into_affine(); //        k_m*G + k_s*R_d
    let a3 = (r_e * k_m - h * k_b).into_affine(); //        k_m*R_e - k_b*H

    let e = transcript(
        &pk_dep,
        &(r_d, c_d),
        &(r_e, c_e),
        &p_i,
        &a2,
        &a3,
        &a4,
        account,
        chainid,
    )
    .e();

    Ok(DepositCouplingProof {
        e: w_from_fr(&e),
        s_m: w_from_fr(&(k_m + e * m)),
        s_s: w_from_fr(&(k_s + e * sk)),
        s_b: w_from_fr(&(k_b + e * b)),
        a2: w_from_g1(&a2),
        a3: w_from_g1(&a3),
        a4: w_from_g1(&a4),
        p_i: w_from_g1(&p_i),
    })
}

/// Verify: some identity scalar binds the deposit account AND decrypts
/// `eIss` to the point committed in `P_I`.
#[allow(clippy::too_many_arguments)]
pub fn deposit_couple_verify(
    pk_dep: &G1w,
    e_dep: &(G1w, G1w),
    e_iss: &(G1w, G1w),
    proof: &DepositCouplingProof,
    account: &W256,
    chainid: &W256,
) -> Result<bool> {
    let pk_dep = g1_from_w(pk_dep)?;
    let r_d = g1_from_w(&e_dep.0)?;
    let c_d = g1_from_w(&e_dep.1)?;
    let r_e = g1_from_w(&e_iss.0)?;
    let c_e = g1_from_w(&e_iss.1)?;
    let p_i = g1_from_w(&proof.p_i)?;
    let a2 = g1_from_w(&proof.a2)?;
    let a3 = g1_from_w(&proof.a3)?;
    let a4 = g1_from_w(&proof.a4)?;
    let e = fr_mod(&proof.e);
    let s_m = fr_mod(&proof.s_m);
    let s_s = fr_mod(&proof.s_s);
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
    // E3: s_m*R_e - s_b*H == A3 + e*(C_e - P_I)
    let x: G1Projective = G1Projective::from(c_e) - p_i;
    if r_e * s_m - h * s_b != x * e + a3 {
        return Ok(false);
    }
    // Fiat-Shamir
    let e_check = transcript(
        &pk_dep,
        &(r_d, c_d),
        &(r_e, c_e),
        &p_i,
        &a2,
        &a3,
        &a4,
        account,
        chainid,
    )
    .e();
    Ok(w_from_fr(&e_check) == proof.e)
}
