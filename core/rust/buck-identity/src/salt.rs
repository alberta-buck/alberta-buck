//! Holder-derived subtree salts -- mirrors `alberta_buck/wallet/salt.py`.
//!
//! A private subtree's leaf hides its identity behind the holder's salt, and
//! the salt is derived from a wallet secret rather than from the identity, so
//! an authority that learns one salt cannot derive another and a wallet
//! restored from its seed recomputes every one (accumulator specification,
//! section 7).  Re-association increments the counter, and old and new leaves
//! share no salt.

use crate::keccak::keccak_raw;
use crate::poseidon::poseidon;
use crate::{reduce_mod_order, IdError, Result, W256, ZERO_W};

/// `keccak("AlbertaBuck/Accumulator/Salt/v2") mod F_R`: keeps a salt out of the
/// range of every other Poseidon preimage the wallet computes.
pub fn salt_domain() -> W256 {
    crate::domains::field_tag(crate::domains::ACCUMULATOR_SALT)
}

/// A subtree identifier as a field element: `keccak(tree_id) mod F_R`.
pub fn tree_tag(tree_id: &str) -> Result<W256> {
    if tree_id.is_empty() {
        return Err(IdError("tree_id must be a non-empty string"));
    }
    Ok(reduce_mod_order(&keccak_raw(tree_id.as_bytes())))
}

/// This holder's salt for `tree_id` at `association_counter`, in `[1, F_R)`.
/// A zero Poseidon output (negligible) steps to the next counter, since a zero
/// salt would make the leaf deterministic.
pub fn derive_salt(holder_secret: &W256, tree_id: &str, association_counter: u64) -> Result<W256> {
    if *holder_secret == ZERO_W {
        return Err(IdError("holder_secret must be a positive int"));
    }
    let tag = tree_tag(tree_id)?;
    let mut counter = association_counter;
    loop {
        let mut c = ZERO_W;
        c[24..].copy_from_slice(&counter.to_be_bytes());
        let salt = poseidon(&[salt_domain(), *holder_secret, tag, c])?;
        if salt != ZERO_W {
            return Ok(salt);
        }
        counter += 1;
    }
}
