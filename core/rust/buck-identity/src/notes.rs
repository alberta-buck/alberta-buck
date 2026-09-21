//! BUCK Notes commitment / nullifier / id-hash family -- mirrors
//! `alberta_buck/wallet/notes.py` (and `identity_leaf` from
//! `alberta_buck/registry/tree.py`).
//!
//! `cm = Poseidon([flavor, v, rho, id_hash, predicate])`;
//! `nf = Poseidon([rho, id_hash, tag])` with tags 4242 (B) / 4243 (A,
//! reserved); id-hashes are Poseidon over the per-flavor payload word
//! layouts, each word reduced mod F_R exactly as circom signals are.

use crate::poseidon::poseidon;
use crate::{w_is_zero, w_lt_order, G1w, IdError, Result, W256};

pub const NULLIFIER_TAG_B: u64 = 4242;
pub const NULLIFIER_TAG_A: u64 = 4243;

pub const FLAVOR_A1: u64 = 1;
pub const FLAVOR_A2: u64 = 2;
pub const FLAVOR_B1: u64 = 3;

pub(crate) fn w_from_u64(v: u64) -> W256 {
    let mut w = [0u8; 32];
    w[24..].copy_from_slice(&v.to_be_bytes());
    w
}

fn check_rho(rho: &W256) -> Result<()> {
    if w_is_zero(rho) || !w_lt_order(rho) {
        return Err(IdError("rho must be a non-zero scalar mod ORDER"));
    }
    Ok(())
}

fn check_field(w: &W256, msg: &'static str) -> Result<()> {
    if !w_lt_order(w) {
        return Err(IdError(msg));
    }
    Ok(())
}

fn check_scalar_nonzero(w: &W256) -> Result<()> {
    if w_is_zero(w) || !w_lt_order(w) {
        return Err(IdError("scalars must lie in [1, ORDER)"));
    }
    Ok(())
}

/// `cm = Poseidon([flavor, v, rho, id_hash, predicate])` with the
/// `NoteOpening` range checks.
pub fn note_commitment(
    flavor: u64,
    v: &W256,
    rho: &W256,
    id_hash: &W256,
    predicate: &W256,
) -> Result<W256> {
    if !matches!(flavor, FLAVOR_A1 | FLAVOR_A2 | FLAVOR_B1) {
        return Err(IdError("unknown flavor"));
    }
    // v in [0, 2^128): the high 16 bytes must be zero.
    if v[..16].iter().any(|b| *b != 0) {
        return Err(IdError("v out of range [0, 2^128)"));
    }
    check_rho(rho)?;
    check_field(id_hash, "id_hash must lie in [0, F_R)")?;
    check_field(predicate, "predicate must lie in [0, F_R)")?;
    poseidon(&[w_from_u64(flavor), *v, *rho, *id_hash, *predicate])
}

/// B-spend nullifier: `Poseidon([rho, id_hash, 4242])`.
pub fn nullifier_b(rho: &W256, id_hash: &W256) -> Result<W256> {
    check_rho(rho)?;
    check_field(id_hash, "id_hash must lie in [0, F_R)")?;
    poseidon(&[*rho, *id_hash, w_from_u64(NULLIFIER_TAG_B)])
}

/// RESERVED A-tag nullifier: `Poseidon([rho, id_hash, 4243])`.
pub fn nullifier_a(rho: &W256, id_hash: &W256) -> Result<W256> {
    check_rho(rho)?;
    check_field(id_hash, "id_hash must lie in [0, F_R)")?;
    poseidon(&[*rho, *id_hash, w_from_u64(NULLIFIER_TAG_A)])
}

/// B1 id-hash: `Poseidon([m_issuer, sigma_R.x, sigma_R.y, sigma_s])`.
pub fn id_hash_b1(m_issuer: &W256, sigma_r: &G1w, sigma_s: &W256) -> Result<W256> {
    check_scalar_nonzero(m_issuer)?;
    check_scalar_nonzero(sigma_s)?;
    poseidon(&[*m_issuer, sigma_r.0, sigma_r.1, *sigma_s])
}

/// A1 id-hash: `Poseidon([E_note.R, E_note.C, m_issuer, sigma_R, sigma_s])`
/// (9 words).
pub fn id_hash_a1(
    e_note: &(G1w, G1w),
    m_issuer: &W256,
    sigma_r: &G1w,
    sigma_s: &W256,
) -> Result<W256> {
    check_scalar_nonzero(m_issuer)?;
    check_scalar_nonzero(sigma_s)?;
    poseidon(&[
        e_note.0 .0,
        e_note.0 .1,
        e_note.1 .0,
        e_note.1 .1,
        *m_issuer,
        sigma_r.0,
        sigma_r.1,
        *sigma_s,
    ])
}

/// A2 id-hash: `Poseidon([E_note.R, E_note.C, E_iss.R, E_iss.C])` (8 words).
pub fn id_hash_a2(e_note: &(G1w, G1w), e_iss: &(G1w, G1w)) -> Result<W256> {
    poseidon(&[
        e_note.0 .0,
        e_note.0 .1,
        e_note.1 .0,
        e_note.1 .1,
        e_iss.0 .0,
        e_iss.0 .1,
        e_iss.1 .0,
        e_iss.1 .1,
    ])
}

/// Identity Merkle leaf: `Poseidon([M.x, M.y])` -- matches
/// `circuits/identity_membership.circom` and `registry/tree.py`.
pub fn identity_leaf(m_point: &G1w) -> Result<W256> {
    poseidon(&[m_point.0, m_point.1])
}

/// Hiding leaf of a PRIVATE subtree: `Poseidon([M.x, M.y, salt])`.
///
/// Membership in a private subtree is a fact about a person who did not
/// publish it, so the leaf must not be a deterministic function of the
/// identity: a registry holding every scalar it ever certified would
/// otherwise decide membership at will.  `salt` MUST lie in `[1, F_R)`;
/// zero is refused because it makes the leaf deterministic.
///
/// Mirrors `alberta_buck/registry/tree.py::identity_leaf_salted`.
pub fn identity_leaf_salted(m_point: &G1w, salt: &W256) -> Result<W256> {
    if !salt_in_range(salt) {
        return Err(IdError(
            "salt must be in [1, F_R); 0 makes the leaf deterministic",
        ));
    }
    poseidon(&[m_point.0, m_point.1, *salt])
}

/// Hiding leaf of a private IDENTITY-REGISTRY subtree, binding the pair:
/// `Poseidon([m_rec, k_recv, salt])`.
///
/// Addressed Notes are keyed to the receiving key `k*G` rather than to the
/// identity point, because an identity scalar is a read capability the
/// design discloses to every counterparty and so cannot also be a
/// decryption key.  That separation obliges the spend to prove the mailbox
/// belongs to the Identity, and this leaf is where the binding lives --
/// committed, never published, because a public binding would deanonymise
/// the recipient at spend.
///
/// It commits the SCALARS where its two siblings commit coordinates, and
/// that difference is principled.  The siblings are computed by authorities
/// holding identity points; this leaf exists to be proven in zero knowledge
/// by a holder that has the scalars.  Committing points would cost the
/// circuit two fixed-base multiplications -- 943,792 constraints -- to
/// re-derive preimages the prover already holds.  BN254's G1 group order
/// equals the Poseidon field, so a scalar is a field element outright.
///
/// Mirrors `alberta_buck/registry/tree.py::receiving_leaf`.
pub fn receiving_leaf(m_rec: &W256, k_recv: &W256, salt: &W256) -> Result<W256> {
    for v in [m_rec, k_recv, salt] {
        if !salt_in_range(v) {
            return Err(IdError(
                "receiving_leaf inputs must each lie in [1, F_R)",
            ));
        }
    }
    poseidon(&[*m_rec, *k_recv, *salt])
}

/// `mailbox_leaf(M, pk_recv, salt) = Poseidon([M.x, M.y, pk.x, pk.y, salt])`
/// -- the PAYER's view of the association `receiving_leaf` commits.
///
/// Two leaves for one fact, because it has two consumers holding different
/// things.  The spend proves the association in zero knowledge and the prover
/// holds the scalars, so `receiving_leaf` commits them and costs one Poseidon.
/// A payer must check the association BEFORE paying and holds no secret at all
/// -- only the two points, which it needs anyway -- so its leaf commits the
/// POINTS and checking it is a hash and a path.  Distinct associations carry
/// distinct salts, so the salt a holder hands a payer does not locate the leaf
/// its spend proves under.
pub fn mailbox_leaf(m_point: &G1w, pk_recv: &G1w, salt: &W256) -> Result<W256> {
    if !salt_in_range(salt) {
        return Err(IdError(
            "salt must be in [1, F_R); 0 makes the leaf deterministic",
        ));
    }
    poseidon(&[m_point.0, m_point.1, pk_recv.0, pk_recv.1, *salt])
}

/// `salt` is a field element in `[1, F_R)`.  Poseidon reduces its inputs
/// mod `F_R`, so an out-of-range salt would alias onto an in-range one;
/// refusing it here keeps the Python and Rust leaves byte-identical.
fn salt_in_range(salt: &W256) -> bool {
    use ark_ff::Zero;
    let s = crate::fr_mod(salt);
    !s.is_zero() && crate::w_from_fr(&s) == *salt
}
