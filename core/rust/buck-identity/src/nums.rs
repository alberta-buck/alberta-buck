//! The one hiding generator, `H_PEDERSEN`, whose discrete log no one knows --
//! mirrors `alberta_buck/wallet/nums.py`, which carries the full argument.
//!
//! Every blind the protocol opens in more than one proof sits on it.  B1's
//! `P_dep = M_dep + b*H` is opened by a sigma and by a membership proof; with a
//! known `h = log_G(H)` a depositor holding any registered scalar `m'` sets
//! `b' = b + (m_dep - m')/h` and the halves name different identities (review
//! finding 5).  The A2 binding's `T = r'*pk + gamma*H` is opened again by the
//! A2 fold; with a known `h` a minter pays any difference of Identities in
//! `gamma` (the A2 key split).  So there is no second, known-log generator.
//!
//! Derivation: try-and-increment, the standard construction for a FIXED public
//! parameter.  Constant-time hashing matters when the input is secret; here the
//! input is a domain string and the output is computed once.

use ark_bn254::{Fq, G1Affine};
use ark_ff::{BigInteger, Field, PrimeField};
use std::sync::OnceLock;

use crate::keccak::keccak_raw;
use crate::{w_from_g1, G1w};

/// Domain separator.  Changing it changes the point.
pub const H_PEDERSEN_DOMAIN: &[u8] = crate::domains::PEDERSEN_H;

/// The Pedersen generator: on the curve, with nobody's knowledge of its
/// discrete log, because it was never computed as a multiple of `G`.
pub(crate) fn h_pedersen_affine() -> G1Affine {
    static H: OnceLock<G1Affine> = OnceLock::new();
    *H.get_or_init(|| {
        for ctr in 0u32..256 {
            let mut pre = H_PEDERSEN_DOMAIN.to_vec();
            pre.extend_from_slice(&ctr.to_be_bytes());
            let x = Fq::from_be_bytes_mod_order(&keccak_raw(&pre));
            let y2 = x * x * x + Fq::from(3u64);
            if let Some(y) = y2.sqrt() {
                // Canonical: the even root, matching the Python derivation.
                let even = if y.into_bigint().is_odd() { -y } else { y };
                let p = G1Affine::new_unchecked(x, even);
                if p.is_on_curve() {
                    return p;
                }
            }
        }
        unreachable!("no curve point under 256 counters")
    })
}

/// `H_PEDERSEN` as a word pair (for bindings and callers).
pub fn h_pedersen() -> G1w {
    w_from_g1(&h_pedersen_affine())
}
