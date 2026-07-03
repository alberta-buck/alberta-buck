//! Pointcheval-Sanders single-message signatures -- mirrors
//! `alberta_buck/wallet/ps.py`.
//!
//! Secret key `(x, y)` scalars; public key `(X, Y) = (x*G2, y*G2)`.
//! Signature on `m`: `sigma = (h, (x + m*y)*h)` for `h = t*G1`.
//! Verification: `e(sigma_1, X + m*Y) == e(sigma_2, g_2)` and
//! `sigma_1 != O`.

use ark_bn254::{G1Affine, G2Affine};
use ark_ec::{AffineRepr, CurveGroup};

use crate::pairing::product_is_one;
use crate::{fr_mod, g1_from_w, g2_from_w, w_from_g1, w_from_g1p, G1w, G2w, Result, W256};

/// `sigma = (h, (x + m*y)*h)` with `h = t*G1` (`t` is the caller's nonce).
pub fn ps_sign(sk_x: &W256, sk_y: &W256, m: &W256, t: &W256) -> Result<(G1w, G1w)> {
    let x = fr_mod(sk_x);
    let y = fr_mod(sk_y);
    let m = fr_mod(m);
    let h = (G1Affine::generator() * fr_mod(t)).into_affine();
    let coeff = x + m * y;
    Ok((w_from_g1(&h), w_from_g1p(&(h * coeff))))
}

/// `e(sigma_1, X + m*Y) == e(sigma_2, g_2)` and `sigma_1 != O`.
pub fn ps_verify(pk_x: &G2w, pk_y: &G2w, sigma_1: &G1w, sigma_2: &G1w, m: &W256) -> Result<bool> {
    let s1 = g1_from_w(sigma_1)?;
    let s2 = g1_from_w(sigma_2)?;
    if s1.is_zero() {
        return Ok(false);
    }
    let x = g2_from_w(pk_x)?;
    let y = g2_from_w(pk_y)?;
    let xmy = (y * fr_mod(m) + x).into_affine();
    Ok(product_is_one(&[(s1, xmy), (-s2, G2Affine::generator())]))
}

/// `sigma' = (t*sigma_1, t*sigma_2)` -- same `m`, uncorrelated.
pub fn ps_rerandomize(sigma_1: &G1w, sigma_2: &G1w, t: &W256) -> Result<(G1w, G1w)> {
    let s1 = g1_from_w(sigma_1)?;
    let s2 = g1_from_w(sigma_2)?;
    let t = fr_mod(t);
    Ok((w_from_g1p(&(s1 * t)), w_from_g1p(&(s2 * t))))
}
