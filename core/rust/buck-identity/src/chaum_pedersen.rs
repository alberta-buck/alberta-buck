//! Chaum-Pedersen NIZK of equal-plaintext re-encryption -- mirrors
//! `alberta_buck/wallet/chaum_pedersen.py` (the approve handshake,
//! on-chain `IdentityRegistry.verifyApprove`).
//!
//! Commitments `T1 = k1*R_a`, `T2 = k2*pk_b`, `T3 = k2*G`; challenge binds
//! `(E_alice, E_bob, pk_alice, pk_bob, T1, T2, T3, sender, spender,
//! chainid)`; responses `s1 = k1 + e*sk_alice`, `s2 = k2 + e*r'`.

use ark_bn254::{G1Affine, G1Projective};
use ark_ec::{AffineRepr, CurveGroup};

use crate::{fr_mod, g1_from_w, w_from_fr, w_from_g1, G1w, Result, Transcript, W256};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CpProof {
    pub e: W256,
    pub s1: W256,
    pub s2: W256,
    pub t1: G1w,
    pub t2: G1w,
    pub t3: G1w,
}

#[allow(clippy::too_many_arguments)]
fn transcript(
    e_alice: &(G1Affine, G1Affine),
    e_bob: &(G1Affine, G1Affine),
    pk_alice: &G1Affine,
    pk_bob: &G1Affine,
    t1: &G1Affine,
    t2: &G1Affine,
    t3: &G1Affine,
    sender: &W256,
    spender: &W256,
    chainid: &W256,
) -> Transcript {
    let mut t = Transcript::new();
    t.p(&e_alice.0)
        .p(&e_alice.1)
        .p(&e_bob.0)
        .p(&e_bob.1)
        .p(pk_alice)
        .p(pk_bob)
        .p(t1)
        .p(t2)
        .p(t3)
        .w(sender)
        .w(spender)
        .w(chainid);
    t
}

#[allow(clippy::too_many_arguments)]
pub fn chaum_pedersen_prove(
    e_alice: &(G1w, G1w),
    e_bob: &(G1w, G1w),
    pk_alice: &G1w,
    pk_bob: &G1w,
    sk_alice: &W256,
    r_prime: &W256,
    sender: &W256,
    spender: &W256,
    chainid: &W256,
    k1: &W256,
    k2: &W256,
) -> Result<CpProof> {
    let ea = (g1_from_w(&e_alice.0)?, g1_from_w(&e_alice.1)?);
    let eb = (g1_from_w(&e_bob.0)?, g1_from_w(&e_bob.1)?);
    let pka = g1_from_w(pk_alice)?;
    let pkb = g1_from_w(pk_bob)?;
    let k1 = fr_mod(k1);
    let k2 = fr_mod(k2);

    let t1 = (ea.0 * k1).into_affine();
    let t2 = (pkb * k2).into_affine();
    let t3 = (G1Affine::generator() * k2).into_affine();

    let e = transcript(&ea, &eb, &pka, &pkb, &t1, &t2, &t3, sender, spender, chainid).e();
    let s1 = k1 + e * fr_mod(sk_alice);
    let s2 = k2 + e * fr_mod(r_prime);

    Ok(CpProof {
        e: w_from_fr(&e),
        s1: w_from_fr(&s1),
        s2: w_from_fr(&s2),
        t1: w_from_g1(&t1),
        t2: w_from_g1(&t2),
        t3: w_from_g1(&t3),
    })
}

#[allow(clippy::too_many_arguments)]
pub fn chaum_pedersen_verify(
    e_alice: &(G1w, G1w),
    e_bob: &(G1w, G1w),
    pk_alice: &G1w,
    pk_bob: &G1w,
    proof: &CpProof,
    sender: &W256,
    spender: &W256,
    chainid: &W256,
) -> Result<bool> {
    let ea = (g1_from_w(&e_alice.0)?, g1_from_w(&e_alice.1)?);
    let eb = (g1_from_w(&e_bob.0)?, g1_from_w(&e_bob.1)?);
    let pka = g1_from_w(pk_alice)?;
    let pkb = g1_from_w(pk_bob)?;
    let t1 = g1_from_w(&proof.t1)?;
    let t2 = g1_from_w(&proof.t2)?;
    let t3 = g1_from_w(&proof.t3)?;
    let e = fr_mod(&proof.e);
    let s1 = fr_mod(&proof.s1);
    let s2 = fr_mod(&proof.s2);

    // Check 1: s2*G == T3 + e*R_b
    if G1Affine::generator() * s2 != eb.0 * e + t3 {
        return Ok(false);
    }

    // Check 2: s1*R_a - s2*pk_b == (T1 - T2) + e*(C_a - C_b)
    let lhs: G1Projective = ea.0 * s1 - pkb * s2;
    let ca_cb: G1Projective = G1Projective::from(ea.1) - eb.1;
    let rhs: G1Projective = ca_cb * e + t1 - t2;
    if lhs != rhs {
        return Ok(false);
    }

    // Check 3: Fiat-Shamir
    let e_check = transcript(&ea, &eb, &pka, &pkb, &t1, &t2, &t3, sender, spender, chainid).e();
    Ok(w_from_fr(&e_check) == proof.e)
}
