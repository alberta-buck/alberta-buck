//! EC-ElGamal over BN254 G1 -- mirrors `alberta_buck/wallet/elgamal.py`.
//!
//! Encrypts a message POINT `M`: ciphertext `(R, C) = (r*G, M + r*pk)`;
//! decryption `M = C - sk*R`.  A ciphertext crosses the wire as its two
//! G1 word pairs `(R, C)`.

use ark_bn254::G1Affine;
use ark_ec::AffineRepr;

use crate::{fr_mod, g1_from_w, w_from_g1p, G1w, Result, W256};

/// `(R, C) = (r*G, M + r*pk)`.
pub fn elgamal_encrypt(m_point: &G1w, pk: &G1w, r: &W256) -> Result<(G1w, G1w)> {
    let m = g1_from_w(m_point)?;
    let pk = g1_from_w(pk)?;
    let r = fr_mod(r);
    let big_r = G1Affine::generator() * r;
    let c = pk * r + m;
    Ok((w_from_g1p(&big_r), w_from_g1p(&c)))
}

/// `M = C - sk*R`.
pub fn elgamal_decrypt(ct_r: &G1w, ct_c: &G1w, sk: &W256) -> Result<G1w> {
    let r = g1_from_w(ct_r)?;
    let c = g1_from_w(ct_c)?;
    let sk = fr_mod(sk);
    Ok(w_from_g1p(&(-(r * sk) + c)))
}
