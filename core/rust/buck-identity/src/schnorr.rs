//! Public-issuer Schnorr batch binding -- mirrors
//! `alberta_buck/wallet/schnorr.py`.
//!
//! The issuer signs `hBatch = keccak256(abi.encodePacked(cms))` under its
//! registered identity key.  Transcript order matches
//! `IdentityRegistry._fsIssuerSchnorr` byte-for-byte: points `(pk_iss, R)`
//! as (X, Y) word pairs, then scalars `(hBatch, issuer, chainid)`.

use ark_bn254::G1Affine;
use ark_ec::{AffineRepr, CurveGroup};

use crate::keccak::keccak_raw;
use crate::{fr_mod, g1_from_w, w_from_fr, w_from_g1, G1w, Result, Transcript, W256};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SchnorrProof {
    pub e: W256,
    pub s: W256,
    pub r: G1w,
}

/// `hBatch = keccak256(abi.encodePacked(cms))` -- NOT reduced mod ORDER
/// (it enters transcripts as a full-width uint256 word).
pub fn batch_commitment(cms: &[W256]) -> W256 {
    let mut packed = Vec::with_capacity(cms.len() * 32);
    for c in cms {
        packed.extend_from_slice(c);
    }
    keccak_raw(&packed)
}

fn transcript(pk: &G1Affine, r: &G1Affine, h_batch: &W256, issuer: &W256, chainid: &W256) -> Transcript {
    let mut t = Transcript::new();
    t.p(pk).p(r).w(h_batch).w(issuer).w(chainid);
    t
}

/// Sign `h_batch` with nonce `k`; `(e, s, R)`.
pub fn issuer_schnorr_sign(
    sk_iss: &W256,
    h_batch: &W256,
    issuer: &W256,
    chainid: &W256,
    k: &W256,
) -> Result<SchnorrProof> {
    let sk = fr_mod(sk_iss);
    let pk = (G1Affine::generator() * sk).into_affine();
    let k = fr_mod(k);
    let r = (G1Affine::generator() * k).into_affine();
    let e = transcript(&pk, &r, h_batch, issuer, chainid).e();
    let s = k + e * sk;
    Ok(SchnorrProof {
        e: w_from_fr(&e),
        s: w_from_fr(&s),
        r: w_from_g1(&r),
    })
}

/// `s*G == R + e*pk_iss` and the Fiat-Shamir recomputation.
pub fn issuer_schnorr_verify(
    pk_iss: &G1w,
    proof: &SchnorrProof,
    h_batch: &W256,
    issuer: &W256,
    chainid: &W256,
) -> Result<bool> {
    let pk = g1_from_w(pk_iss)?;
    let r = g1_from_w(&proof.r)?;
    let e = fr_mod(&proof.e);
    let s = fr_mod(&proof.s);

    // Check 1: s*G == R + e*pk_iss
    if G1Affine::generator() * s != pk * e + r {
        return Ok(false);
    }
    // Check 2: Fiat-Shamir
    let e_check = transcript(&pk, &r, h_batch, issuer, chainid).e();
    Ok(w_from_fr(&e_check) == proof.e)
}
