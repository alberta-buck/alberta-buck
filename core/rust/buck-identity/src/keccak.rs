//! Fiat-Shamir transcript hashing -- mirrors `alberta_buck/wallet/transcript.py`.
//!
//! `keccak_words` packs every input as a 32-byte big-endian word and hashes
//! (Solidity `keccak256(abi.encodePacked(uint256, ...))`); `keccak_scalar`
//! reduces that digest mod ORDER for use as a challenge.  `keccak_raw` is a
//! plain keccak256 over arbitrary bytes, used by [`identity_scalar`] to hash
//! the canonical identity-data JSON.

use tiny_keccak::{Hasher, Keccak};

use crate::{w_from_fr, W256};
use ark_ff::PrimeField;

pub fn keccak_raw(data: &[u8]) -> [u8; 32] {
    let mut k = Keccak::v256();
    k.update(data);
    let mut out = [0u8; 32];
    k.finalize(&mut out);
    out
}

/// keccak256(concat(words)) -- each word is already 32-byte big-endian.
pub fn keccak_words(words: &[W256]) -> [u8; 32] {
    let mut k = Keccak::v256();
    for w in words {
        k.update(w);
    }
    let mut out = [0u8; 32];
    k.finalize(&mut out);
    out
}

/// `keccak_words` reduced mod ORDER -- the Fiat-Shamir challenge form.
pub fn keccak_scalar(words: &[W256]) -> W256 {
    w_from_fr(&ark_bn254::Fr::from_be_bytes_mod_order(&keccak_words(
        words,
    )))
}

/// `m = keccak256(canonical_identity_data) mod ORDER`.
///
/// The caller supplies the canonical UTF-8 JSON bytes (sorted keys, compact
/// separators) -- canonicalization stays language-side.
pub fn identity_scalar(canonical: &[u8]) -> W256 {
    w_from_fr(&ark_bn254::Fr::from_be_bytes_mod_order(&keccak_raw(
        canonical,
    )))
}
