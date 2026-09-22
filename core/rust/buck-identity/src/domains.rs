//! Every domain tag the v2 protocol hashes, in one place.
//!
//! Mirrors `alberta_buck/wallet/domains.py` and the constants in
//! `src/IdentityRegistry.sol`; a tag changed in one and not the others is a
//! transcript that verifies nowhere, which the kernel vectors catch.
//!
//! Convention: `AlbertaBuck/<Area>/<Name>/v2`, one protocol version rather
//! than a revision count per primitive.  A Fiat-Shamir transcript carries one
//! domain word, `keccak256(tag)`, at a fixed position -- LAST for every
//! transcript that gained a tag in v2, the position the registration
//! transcript already used.  Derivations hash the tag as bytes ahead of their
//! payload.

use crate::keccak::keccak_raw;
use crate::W256;

/// The full keccak word of a tag, as a transcript entry (NOT reduced mod
/// ORDER: metadata folded into a hash, not a scalar).
pub fn word(tag: &[u8]) -> W256 {
    keccak_raw(tag)
}

/// `m = keccak(IDENTITY_SCALAR || canonical record) mod ORDER`.
pub const IDENTITY_SCALAR: &[u8] = b"AlbertaBuck/Identity/Scalar/v2";

pub const FS_REGISTER: &[u8] = b"AlbertaBuck/FiatShamir/IdentityRegistry/Register/v2";
pub const FS_CONTRACT_BINDING: &[u8] =
    b"AlbertaBuck/FiatShamir/IdentityRegistry/ContractBinding/v2";
pub const FS_APPROVE: &[u8] = b"AlbertaBuck/FiatShamir/IdentityRegistry/Approve/v2";
pub const FS_ISSUER_SCHNORR: &[u8] =
    b"AlbertaBuck/FiatShamir/IdentityRegistry/IssuerSchnorr/v2";
pub const FS_ISSUER_REENC: &[u8] = b"AlbertaBuck/FiatShamir/IdentityRegistry/IssuerReenc/v2";
pub const FS_DEPOSITOR_BINDING: &[u8] =
    b"AlbertaBuck/FiatShamir/IdentityRegistry/DepositorBinding/v2";
/// Verified off chain, by a receipt checker.
pub const FS_VERIFIABLE_DECRYPT: &[u8] = b"AlbertaBuck/FiatShamir/Receipt/VerifiableDecrypt/v2";

pub const CONTRACT_BINDING_CONTROL: &[u8] =
    b"AlbertaBuck/IdentityRegistry/ContractBindingControl/v2";

pub const ACCUMULATOR_SALT: &[u8] = b"AlbertaBuck/Accumulator/Salt/v2";
pub const NOTES_RECEIVING_KEY: &[u8] = b"AlbertaBuck/Notes/ReceivingKey/v2";
pub const NOTES_PAYLOAD_WRAP: &[u8] = b"AlbertaBuck/Notes/PayloadWrap/v2";

pub const RECEIPT_ENVELOPE: &str = "AB-RCPT/2";
