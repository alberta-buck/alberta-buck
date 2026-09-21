//! Nothing-up-my-sleeve generators, and the one whose discrete log must be
//! unknown -- mirrors `alberta_buck/wallet/nums.py`.
//!
//! The system uses a second G1 generator in two different ways with opposite
//! requirements, and conflating them is what review finding 5 caught.
//!
//! `issuer_reenc::h_point` masks a value inside ONE sigma, where a knowledge
//! extractor recovers both openings, so a known discrete log costs nothing and
//! that generator is simply `keccak(domain)*G`.
//!
//! `h_pedersen` is for the other use: a commitment opened by two SEPARATE
//! proofs that must agree.  B1 publishes `P_dep = M_dep + b*H` and then proves
//! two things about it -- a sigma opening it as `m_dep*G + b*H`, and a
//! membership proof opening it as `M + b'*H` for a registered `M`.  With a
//! known `h = log_G(H)` those openings need not agree: a depositor holding any
//! registered identity scalar `m'` sets `b' = b + (m_dep - m')/h`, and an
//! unregistered depositor spends.  Hashing to the curve leaves no such `h`.
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
pub const H_PEDERSEN_DOMAIN: &[u8] = b"AlbertaBuck/Pedersen/H/v1";

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
