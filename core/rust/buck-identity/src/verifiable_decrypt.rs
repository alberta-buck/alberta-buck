//! Verifiable decryption -- mirrors `alberta_buck/wallet/verifiable_decrypt.py`.
//!
//! Chaum-Pedersen DLEQ on bases `(G, R)`: proves `E = (R, C)` decrypts to
//! the REVEALED point `M` under `pk = sk*G`, without exposing `sk`.

use ark_bn254::{G1Affine, G1Projective};
use ark_ec::{AffineRepr, CurveGroup};

use crate::{fr_mod, g1_from_w, w_from_fr, w_from_g1, G1w, Result, Transcript, W256};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VdProof {
    pub e: W256,
    pub s: W256,
    pub t1: G1w,
    pub t2: G1w,
}

#[allow(clippy::too_many_arguments)]
fn transcript(
    e_r: &G1Affine,
    e_c: &G1Affine,
    pk: &G1Affine,
    m: &G1Affine,
    t1: &G1Affine,
    t2: &G1Affine,
    account: &W256,
    chainid: &W256,
) -> Transcript {
    let mut t = Transcript::new();
    t.p(e_r).p(e_c).p(pk).p(m).p(t1).p(t2).w(account).w(chainid)
        .w(&crate::domains::word(crate::domains::FS_VERIFIABLE_DECRYPT));
    t
}

/// Prove `E` decrypts to `M` under `pk = sk*G`, with the caller's nonce `t`.
pub fn verifiable_decrypt_prove(
    e_ct: &(G1w, G1w),
    sk: &W256,
    m_point: &G1w,
    account: &W256,
    chainid: &W256,
    t: &W256,
) -> Result<VdProof> {
    let er = g1_from_w(&e_ct.0)?;
    let ec = g1_from_w(&e_ct.1)?;
    let m = g1_from_w(m_point)?;
    let sk = fr_mod(sk);
    let t = fr_mod(t);

    let pk = (G1Affine::generator() * sk).into_affine();
    let t1 = (G1Affine::generator() * t).into_affine();
    let t2 = (er * t).into_affine();
    let e = transcript(&er, &ec, &pk, &m, &t1, &t2, account, chainid).e();
    let s = t + e * sk;

    Ok(VdProof {
        e: w_from_fr(&e),
        s: w_from_fr(&s),
        t1: w_from_g1(&t1),
        t2: w_from_g1(&t2),
    })
}

/// True iff `M` is exactly the decryption of `E` under `pk`.
#[allow(clippy::too_many_arguments)]
pub fn verifiable_decrypt_verify(
    e_ct: &(G1w, G1w),
    pk: &G1w,
    m_point: &G1w,
    proof: &VdProof,
    account: &W256,
    chainid: &W256,
) -> Result<bool> {
    let er = g1_from_w(&e_ct.0)?;
    let ec = g1_from_w(&e_ct.1)?;
    let pk = g1_from_w(pk)?;
    let m = g1_from_w(m_point)?;
    let t1 = g1_from_w(&proof.t1)?;
    let t2 = g1_from_w(&proof.t2)?;
    let e = fr_mod(&proof.e);
    let s = fr_mod(&proof.s);

    // X2 = C - M
    let x2: G1Projective = G1Projective::from(ec) - m;

    // Check 1: s*G == T1 + e*pk
    if G1Affine::generator() * s != pk * e + t1 {
        return Ok(false);
    }
    // Check 2: s*R == T2 + e*(C - M)
    if er * s != x2 * e + t2 {
        return Ok(false);
    }
    // Check 3: Fiat-Shamir
    let e_check = transcript(&er, &ec, &pk, &m, &t1, &t2, account, chainid).e();
    Ok(w_from_fr(&e_check) == proof.e)
}
