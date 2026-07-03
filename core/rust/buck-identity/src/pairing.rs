//! Pairing product check -- the EVM `ecPairing` precompile semantics.
//!
//! `pairing_check(pairs)` returns `prod e(P_i, Q_i) == 1`, which is exactly
//! how every pairing equation in the wallet is phrased on chain (negate the
//! G1 side of the terms that sit on the other side of the equality).

use ark_bn254::{Bn254, G1Affine, G2Affine};
use ark_ec::pairing::Pairing;
use ark_ff::One;

use crate::{g1_from_w, g2_from_w, G1w, G2w, Result};

pub(crate) fn product_is_one(pairs: &[(G1Affine, G2Affine)]) -> bool {
    let g1s: Vec<G1Affine> = pairs.iter().map(|p| p.0).collect();
    let g2s: Vec<G2Affine> = pairs.iter().map(|p| p.1).collect();
    let ml = Bn254::multi_miller_loop(g1s, g2s);
    match Bn254::final_exponentiation(ml) {
        Some(out) => out.0.is_one(),
        None => false,
    }
}

/// `prod e(P_i, Q_i) == 1` over validated points.
pub fn pairing_check(pairs: &[(G1w, G2w)]) -> Result<bool> {
    let mut v = Vec::with_capacity(pairs.len());
    for (p, q) in pairs {
        v.push((g1_from_w(p)?, g2_from_w(q)?));
    }
    Ok(product_is_one(&v))
}
