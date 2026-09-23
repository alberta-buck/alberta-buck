//! The Notes receiving key -- mirrors `alberta_buck/wallet/recvkey.py`.
//!
//! An addressed note names an Identity and is KEYED to that Identity's
//! receiving key.  The identity scalar is a read capability the design hands to
//! every counterparty, so it cannot also be what opens the recipient's mail:
//! the receiving secret `k` derives from the wallet seed instead, and only
//! `pk_recv = k*G` is ever given out.
//!
//! The payload wrap masks each secret scalar a delivery must carry, under a
//! Diffie-Hellman point both sides derive without a round trip -- the minter
//! from what it chose (`r * pk_recv`), the recipient from what it holds
//! (`k * R`) -- and one mask per field, keyed by the field's name, because one
//! pad over several scalars leaks their differences.

use ark_bn254::Fr;
use ark_ff::PrimeField;

use crate::domains::{NOTES_PAYLOAD_WRAP, NOTES_RECEIVING_KEY};
use crate::keccak::keccak_raw;
use crate::{fr_mod, g1_generator, g1_mul, w_from_fr, G1w, IdError, Result, W256, ZERO_W};

fn word_of_u64(x: u64) -> W256 {
    let mut w = [0u8; 32];
    w[24..].copy_from_slice(&x.to_be_bytes());
    w
}

/// `k = keccak256(NOTES_RECEIVING_KEY || (seed mod ORDER) || rotation) mod
/// ORDER`, stepping to the next rotation in the negligible case that it is 0 --
/// a zero secret would make `pk_recv` the point at infinity.
pub fn derive_receiving_secret(seed: &W256, rotation: u64) -> Result<W256> {
    if *seed == ZERO_W {
        return Err(IdError("seed must be a positive int"));
    }
    let seed_mod = w_from_fr(&fr_mod(seed));
    let mut rot = rotation;
    loop {
        let mut buf = Vec::with_capacity(NOTES_RECEIVING_KEY.len() + 64);
        buf.extend_from_slice(NOTES_RECEIVING_KEY);
        buf.extend_from_slice(&seed_mod);
        buf.extend_from_slice(&word_of_u64(rot));
        let k = w_from_fr(&Fr::from_be_bytes_mod_order(&keccak_raw(&buf)));
        if k != ZERO_W {
            return Ok(k);
        }
        rot = rot.checked_add(1).ok_or(IdError("rotation overflow"))?;
    }
}

/// `pk_recv = k*G` -- the address a payer encrypts to.
pub fn receiving_public(k: &W256) -> Result<G1w> {
    g1_mul(&g1_generator(), k)
}

/// The one-time mask for ONE payload field:
/// `keccak(NOTES_PAYLOAD_WRAP || "/" || label || "/" || S.x || S.y) mod ORDER`.
pub fn wrap_mask(shared: &G1w, label: &[u8]) -> W256 {
    let mut buf = Vec::with_capacity(NOTES_PAYLOAD_WRAP.len() + label.len() + 66);
    buf.extend_from_slice(NOTES_PAYLOAD_WRAP);
    buf.push(b'/');
    buf.extend_from_slice(label);
    buf.push(b'/');
    buf.extend_from_slice(&shared.0);
    buf.extend_from_slice(&shared.1);
    w_from_fr(&Fr::from_be_bytes_mod_order(&keccak_raw(&buf)))
}

/// The shared point as the MINTER computes it: `r * pk_recv`.
pub fn mailbox_shared_minter(r: &W256, pk_recv: &G1w) -> Result<G1w> {
    g1_mul(pk_recv, r)
}

/// The shared point as the RECIPIENT computes it: `k * R`.
pub fn mailbox_shared_recipient(k: &W256, r_point: &G1w) -> Result<G1w> {
    g1_mul(r_point, k)
}

/// Mask a payload scalar to the mailbox.  Inverse of [`unwrap_scalar`].
pub fn wrap_scalar(value: &W256, shared: &G1w, label: &[u8]) -> W256 {
    w_from_fr(&(fr_mod(value) + fr_mod(&wrap_mask(shared, label))))
}

/// Recover a wrapped payload scalar.  The label MUST match the wrap's.
pub fn unwrap_scalar(blob: &W256, shared: &G1w, label: &[u8]) -> W256 {
    w_from_fr(&(fr_mod(blob) - fr_mod(&wrap_mask(shared, label))))
}
