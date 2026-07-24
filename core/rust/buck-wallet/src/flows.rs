//! Unilateral identity-targeted Note flows -- mirror
//! `alberta_buck/wallet/unilateral_a2.py` and `unilateral_a1.py`.
//!
//! A2 (addressed, private issuer): the issuer encrypts its own registered
//! identity under the recipient's identity POINT; the recipient alone
//! can produce a plaintext receipt naming both parties, with the
//! anti-framing binding and registry-tree membership closing the
//! collusion gaps.  A1 (addressed, public issuer) reuses the same
//! machinery with `eRec` encrypting the recipient identity under itself.
//!
//! Nonce order per function matches the Python reference's rng draws
//! exactly (documented on each signature).

use buck_identity::elgamal::{elgamal_decrypt, elgamal_encrypt};
use buck_identity::issuer_reenc::{issuer_reenc_prove, issuer_reenc_verify, IssuerReencProof};
use buck_identity::notes::{id_hash_a1, id_hash_a2, FLAVOR_A1, FLAVOR_A2};
use buck_identity::verifiable_decrypt::{
    verifiable_decrypt_prove, verifiable_decrypt_verify, VdProof,
};
use buck_identity::{g1_generator, g1_mul, reduce_mod_order};
use buck_registry::tree::IdentityMerkleTree;

use crate::{Ctw, G1w, NoteOpening, Result, W256};

// ---------------------------------------------------------------------------
// A2: mint
// ---------------------------------------------------------------------------

/// Everything the issuer produces for one identity-targeted A2 note.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MintedA2 {
    pub e_note: Ctw,
    pub e_iss: Ctw,
    pub m_i: G1w,
    pub id_hash: W256,
    pub cm: W256,
    pub opening: NoteOpening,
    pub binding: IssuerReencProof,
    pub r_prime: W256,
    pub r_note: W256,
}

/// Issuer mints an A2 note addressed to identity point `m_rec_pt`.
///
/// Nonce order (the Python draw order with no defaults supplied):
/// `r_prime, r_note, beta, gamma, k_r, k_b, k_s, k_g`.
#[allow(clippy::too_many_arguments)]
pub fn mint_unilateral_a2(
    sk_iss: &W256,
    e_reg: &Ctw,
    m_rec_pt: &G1w,
    v: &W256,
    rho: &W256,
    issuer: &W256,
    chainid: &W256,
    predicate: &W256,
    r_prime: &W256,
    r_note: &W256,
    beta: &W256,
    gamma: &W256,
    k_r: &W256,
    k_b: &W256,
    k_s: &W256,
    k_g: &W256,
) -> Result<MintedA2> {
    let r_prime = reduce_mod_order(r_prime);

    // The issuer's registered identity, recovered from its credential.
    let m_i = elgamal_decrypt(&e_reg.0, &e_reg.1, sk_iss)?;

    // eNote = (r_n*G, v*G + r_n*M_rec).
    let v_pt = g1_mul(&g1_generator(), v)?;
    let e_note = elgamal_encrypt(&v_pt, m_rec_pt, r_note)?;

    // eIss = (r'*G, M_I + r'*M_rec).
    let e_iss = elgamal_encrypt(&m_i, m_rec_pt, &r_prime)?;

    // Anti-framing binding, with pk_rec := the identity point M_rec.
    let binding = issuer_reenc_prove(
        sk_iss, &r_prime, m_rec_pt, e_reg, &e_iss, issuer, chainid, beta, gamma, k_r, k_b, k_s,
        k_g,
    )?;

    let id_hash = id_hash_a2(&e_note, &e_iss)?;
    let opening = NoteOpening {
        flavor: FLAVOR_A2,
        v: *v,
        rho: *rho,
        id_hash,
        predicate: *predicate,
    };
    let cm = opening.commitment()?;
    Ok(MintedA2 {
        e_note,
        e_iss,
        m_i,
        id_hash,
        cm,
        opening,
        binding,
        r_prime,
        r_note: reduce_mod_order(r_note),
    })
}

// ---------------------------------------------------------------------------
// A2: receipt
// ---------------------------------------------------------------------------

/// The plaintext, third-party-checkable receipt the recipient alone
/// produces, naming both identities.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnilateralReceipt {
    pub m_i: G1w,
    pub m_rec: G1w,
    pub value: W256,
    pub e_iss: Ctw,
    pub vd: VdProof,
    pub binding: IssuerReencProof,
    pub issuer: W256,
    pub chainid: W256,
    pub m_i_member: bool,
    pub m_rec_member: bool,
}

/// Outcome of a unilateral receipt verification.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UaRcptResult {
    pub valid: bool,
    pub issuer_m: Option<G1w>,
    pub recipient_m: Option<G1w>,
    pub value: W256,
    pub reason: String,
}

/// Recipient produces the A2 receipt from `m_rec` and the note.  One
/// nonce: the verifiable-decryption `t_vd`.
pub fn make_receipt_a2(
    m_rec: &W256,
    minted: &MintedA2,
    issuer: &W256,
    chainid: &W256,
    tree: &IdentityMerkleTree,
    t_vd: &W256,
) -> Result<UnilateralReceipt> {
    let m_rec_pt = g1_mul(&g1_generator(), m_rec)?;
    let e_iss = minted.e_iss;
    let m_i = elgamal_decrypt(&e_iss.0, &e_iss.1, m_rec)?;
    let vd = verifiable_decrypt_prove(&e_iss, m_rec, &m_i, issuer, chainid, t_vd)?;
    Ok(UnilateralReceipt {
        m_i,
        m_rec: m_rec_pt,
        value: minted.opening.v,
        e_iss,
        vd,
        binding: minted.binding.clone(),
        issuer: *issuer,
        chainid: *chainid,
        m_i_member: tree.contains_identity(&m_i, None)?,
        m_rec_member: tree.contains_identity(&m_rec_pt, None)?,
    })
}

/// Third-party verify of an A2 unilateral receipt, no secret.
pub fn verify_receipt_a2(
    receipt: &UnilateralReceipt,
    pk_iss: &G1w,
    e_reg_iss: &Ctw,
    identity_root: &W256,
    tree: &IdentityMerkleTree,
) -> Result<UaRcptResult> {
    let fail = |reason: &str| UaRcptResult {
        valid: false,
        issuer_m: None,
        recipient_m: None,
        value: receipt.value,
        reason: reason.to_string(),
    };
    let e_iss = &receipt.e_iss;

    // (1) Mint anti-framing.
    if !issuer_reenc_verify(
        pk_iss,
        e_reg_iss,
        e_iss,
        &receipt.binding,
        &receipt.issuer,
        &receipt.chainid,
    )? {
        return Ok(fail("issuer binding invalid"));
    }

    // (2) Recipient's verifiable decryption (key = the identity point).
    if !verifiable_decrypt_verify(
        e_iss,
        &receipt.m_rec,
        &receipt.m_i,
        &receipt.vd,
        &receipt.issuer,
        &receipt.chainid,
    )? {
        return Ok(fail("verifiable decryption invalid"));
    }

    // (3) The decrypted issuer identity is registered (the coupling).
    if !tree.contains_identity(&receipt.m_i, Some(identity_root))? {
        return Ok(fail("issuer M not a registered identity"));
    }

    // (4) The recipient identity is registered.
    if !tree.contains_identity(&receipt.m_rec, Some(identity_root))? {
        return Ok(fail("recipient M not a registered identity"));
    }

    Ok(UaRcptResult {
        valid: true,
        issuer_m: Some(receipt.m_i),
        recipient_m: Some(receipt.m_rec),
        value: receipt.value,
        reason: "VALID".to_string(),
    })
}

// ---------------------------------------------------------------------------
// A1: mint + receipt
// ---------------------------------------------------------------------------

/// Everything the (public) issuer produces for one A1 note.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MintedA1 {
    pub e_note: Ctw,
    pub e_rec: Ctw,
    pub id_hash: W256,
    pub cm: W256,
    pub opening: NoteOpening,
    pub r_prime: W256,
    pub r_note: W256,
}

/// Public issuer mints an A1 note addressed to `m_rec_pt`.  Nonce order:
/// `r_prime, r_note`.
#[allow(clippy::too_many_arguments)]
pub fn mint_unilateral_a1(
    m_rec_pt: &G1w,
    v: &W256,
    rho: &W256,
    m_issuer: &W256,
    sigma_r: &G1w,
    sigma_s: &W256,
    predicate: &W256,
    r_prime: &W256,
    r_note: &W256,
) -> Result<MintedA1> {
    let r_prime = reduce_mod_order(r_prime);

    let v_pt = g1_mul(&g1_generator(), v)?;
    let e_note = elgamal_encrypt(&v_pt, m_rec_pt, r_note)?;

    // eRec = (r'*G, M_rec + r'*M_rec): the recipient identity under itself.
    let e_rec = elgamal_encrypt(m_rec_pt, m_rec_pt, &r_prime)?;

    let id_hash = id_hash_a1(&e_note, m_issuer, sigma_r, sigma_s)?;
    let opening = NoteOpening {
        flavor: FLAVOR_A1,
        v: *v,
        rho: *rho,
        id_hash,
        predicate: *predicate,
    };
    let cm = opening.commitment()?;
    Ok(MintedA1 {
        e_note,
        e_rec,
        id_hash,
        cm,
        opening,
        r_prime,
        r_note: reduce_mod_order(r_note),
    })
}

/// The A1 plaintext receipt the recipient alone produces.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct A1Receipt {
    pub m_iss: G1w,
    pub m_rec: G1w,
    pub value: W256,
    pub e_rec: Ctw,
    pub vd: VdProof,
    pub issuer: W256,
    pub chainid: W256,
    pub m_iss_member: bool,
    pub m_rec_member: bool,
}

/// Recipient produces the A1 receipt.  One nonce: `t_vd`.
pub fn make_receipt_a1(
    m_rec: &W256,
    minted: &MintedA1,
    m_iss_pt: &G1w,
    issuer: &W256,
    chainid: &W256,
    tree: &IdentityMerkleTree,
    t_vd: &W256,
) -> Result<A1Receipt> {
    let m_rec_pt = g1_mul(&g1_generator(), m_rec)?;
    let e_rec = minted.e_rec;
    let vd = verifiable_decrypt_prove(&e_rec, m_rec, &m_rec_pt, issuer, chainid, t_vd)?;
    Ok(A1Receipt {
        m_iss: *m_iss_pt,
        m_rec: m_rec_pt,
        value: minted.opening.v,
        e_rec,
        vd,
        issuer: *issuer,
        chainid: *chainid,
        m_iss_member: tree.contains_identity(m_iss_pt, None)?,
        m_rec_member: tree.contains_identity(&m_rec_pt, None)?,
    })
}

/// Third-party verify of an A1 receipt, no secret.
pub fn verify_receipt_a1(
    receipt: &A1Receipt,
    identity_root: &W256,
    tree: &IdentityMerkleTree,
) -> Result<UaRcptResult> {
    let fail = |reason: &str| UaRcptResult {
        valid: false,
        issuer_m: None,
        recipient_m: None,
        value: receipt.value,
        reason: reason.to_string(),
    };

    // (1) eRec decrypts under M_rec to M_rec: the note addressed this
    //     recipient's identity.
    if !verifiable_decrypt_verify(
        &receipt.e_rec,
        &receipt.m_rec,
        &receipt.m_rec,
        &receipt.vd,
        &receipt.issuer,
        &receipt.chainid,
    )? {
        return Ok(fail("verifiable decryption invalid"));
    }

    // (2) The recipient identity is registered.
    if !tree.contains_identity(&receipt.m_rec, Some(identity_root))? {
        return Ok(fail("recipient M not a registered identity"));
    }

    // (3) The public issuer identity is registered.
    if !tree.contains_identity(&receipt.m_iss, Some(identity_root))? {
        return Ok(fail("issuer M not a registered identity"));
    }

    Ok(UaRcptResult {
        valid: true,
        issuer_m: Some(receipt.m_iss),
        recipient_m: Some(receipt.m_rec),
        value: receipt.value,
        reason: "VALID".to_string(),
    })
}
