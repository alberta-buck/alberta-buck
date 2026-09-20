//! Pointcheval-Sanders single-message signatures and the A' hiding
//! presentation -- mirrors `alberta_buck/wallet/ps.py`.
//!
//! Secret key `(x, y)` scalars; public key `(X, Y) = (x*G2, y*G2)` plus the
//! G1 image `Y1 = y*G` that holders blind with.
//! Signature on `m`: `sigma = (h, (x + m*y)*h)` for `h = t*G1`.
//! Verification (raw credential, wallet side):
//! `e(sigma_1, X + m*Y) == e(sigma_2, g_2)` and `sigma_1 != O`.
//!
//! Rerandomization `(t*sigma_1, t*sigma_2)` is wallet-internal only: the
//! result is still a verifiable signature on `m`, so anyone holding a
//! candidate `m` can test it.  What the wallet publishes is the
//! presentation `(A, B) = (a*sigma_1, a*sigma_2 + b*Y1)` for fresh nonzero
//! `a, b`: a uniform G1 pair, `B = x*A + y*(m*A + b*G)`, independent of `m`.

use ark_bn254::{G1Affine, G2Affine};
use ark_ec::{AffineRepr, CurveGroup};
use ark_ff::Zero;

use crate::pairing::product_is_one;
use crate::{
    fr_mod, g1_from_w, g2_from_w, w_from_g1, w_from_g1p, G1w, G2w, IdError, Result, W256,
};

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
///
/// Verifies a RAW credential.  Applied to a published presentation it is
/// false for every `m`; that is the point of the presentation.
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

/// `sigma' = (t*sigma_1, t*sigma_2)` -- same `m`, uncorrelated.  Wallet-internal.
pub fn ps_rerandomize(sigma_1: &G1w, sigma_2: &G1w, t: &W256) -> Result<(G1w, G1w)> {
    let s1 = g1_from_w(sigma_1)?;
    let s2 = g1_from_w(sigma_2)?;
    let t = fr_mod(t);
    Ok((w_from_g1p(&(s1 * t)), w_from_g1p(&(s2 * t))))
}

/// The hiding presentation `(A, B) = (a*sigma_1, a*sigma_2 + b*Y1)`.
///
/// `a` and `b` are the caller's fresh nonzero scalars (Python draw order:
/// `a`, then `b`); `b` is a witness of the registration NIZK.
pub fn ps_present(
    sigma_1: &G1w,
    sigma_2: &G1w,
    y1: &G1w,
    a: &W256,
    b: &W256,
) -> Result<(G1w, G1w)> {
    let s1 = g1_from_w(sigma_1)?;
    let s2 = g1_from_w(sigma_2)?;
    let y1 = g1_from_w(y1)?;
    let a = fr_mod(a);
    let b = fr_mod(b);
    if a.is_zero() || b.is_zero() {
        return Err(IdError("presentation scalars a, b must be nonzero"));
    }
    Ok((w_from_g1p(&(s1 * a)), w_from_g1p(&(s2 * a + y1 * b))))
}

/// `e(Y1, g_2) == e(G, Y)`: the G1 key component matches the G2 one.
pub fn ps_key_consistent(pk_y: &G2w, y1: &G1w) -> Result<bool> {
    let y1 = g1_from_w(y1)?;
    if y1.is_zero() {
        return Ok(false);
    }
    let y = g2_from_w(pk_y)?;
    Ok(product_is_one(&[
        (y1, G2Affine::generator()),
        (-G1Affine::generator(), y),
    ]))
}
