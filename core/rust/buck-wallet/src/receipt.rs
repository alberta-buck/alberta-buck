//! Off-chain non-deniable-receipt verifiers -- mirrors
//! `alberta_buck/wallet/receipt.py`.
//!
//! Two payment paths, two receipt shapes, one idiom: a party assembles a
//! receipt from data they hold plus public chain state, and any third
//! party re-checks it with no secret, naming the counterparty's
//! registered Identity.

use std::collections::BTreeMap;

use buck_identity::chaum_pedersen::{chaum_pedersen_verify, CpProof};
use buck_identity::notes::{nullifier_a, nullifier_b, FLAVOR_A1, FLAVOR_A2};
use buck_identity::schnorr::{batch_commitment, issuer_schnorr_verify, SchnorrProof};
use buck_identity::verifiable_decrypt::{verifiable_decrypt_verify, VdProof};

use crate::{Ctw, G1w, NoteOpening, Result, W256};

/// An IdentityRegistry record as read by a receipt verifier.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RegisteredIdentity {
    pub is_public: bool,
    pub pk: G1w,
    pub m_point: Option<G1w>,
    pub e_addr: Option<Ctw>,
}

/// A non-deniable receipt for one Note payment from a public issuer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Receipt {
    pub opening: NoteOpening,
    pub cms: Vec<W256>,
    pub issuer: W256,
    pub issuer_sig: SchnorrProof,
    pub chainid: W256,
    pub nullifier: W256,
    pub face: W256,
    pub recipient: W256,
}

/// Outcome of a receipt verification.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RcptResult {
    pub ok: bool,
    pub identity_m: Option<G1w>,
    pub value: Option<W256>,
    pub reason: String,
}

impl RcptResult {
    pub fn fail(reason: impl Into<String>) -> Self {
        RcptResult {
            ok: false,
            identity_m: None,
            value: None,
            reason: reason.into(),
        }
    }
}

/// Deterministic nullifier of an opening, dispatched on flavor tag:
/// A-flavor uses tag 4243, B-flavor 4242.
pub fn nullifier_for(opening: &NoteOpening) -> Result<W256> {
    if opening.flavor == FLAVOR_A1 || opening.flavor == FLAVOR_A2 {
        nullifier_a(&opening.rho, &opening.id_hash)
    } else {
        nullifier_b(&opening.rho, &opening.id_hash)
    }
}

/// Verify a public-issuer receipt, naming the payer's registered
/// Identity.  `registry` maps issuer address words to registry reads.
pub fn receipt_verify(
    receipt: &Receipt,
    registry: &BTreeMap<W256, RegisteredIdentity>,
) -> Result<RcptResult> {
    let o = &receipt.opening;

    if o.flavor == FLAVOR_A2 {
        return Ok(RcptResult::fail(
            "(c) A2 private-issuer receipt requires Phase 2 in-SNARK binding",
        ));
    }

    // (a) OPENING: recompute cm and require it in the batch.
    let cm = o.commitment()?;
    if !receipt.cms.contains(&cm) {
        return Ok(RcptResult::fail("(a) opening cm not in minted batch"));
    }

    // (c) ISSUER: registry read; must be a public Identity with M known.
    let Some(rec) = registry.get(&receipt.issuer) else {
        return Ok(RcptResult::fail("(c) issuer not registered"));
    };
    if !rec.is_public {
        return Ok(RcptResult::fail("(c) issuer is not a public Identity"));
    }
    let Some(m_iss) = rec.m_point else {
        return Ok(RcptResult::fail("(c) public issuer Identity M unavailable"));
    };

    // (b)+(c): the batch Schnorr binds the registered key to keccak(cms).
    let h_batch = batch_commitment(&receipt.cms);
    if !issuer_schnorr_verify(
        &rec.pk,
        &receipt.issuer_sig,
        &h_batch,
        &receipt.issuer,
        &receipt.chainid,
    )? {
        return Ok(RcptResult::fail("(b)/(c) issuer batch binding fails"));
    }

    // (d) PAID: nullifier and face are deterministic in the opening.
    if nullifier_for(o)? != receipt.nullifier {
        return Ok(RcptResult::fail("(d) nullifier does not match opening"));
    }
    if receipt.face != o.v {
        return Ok(RcptResult::fail("(d) paid face != note value"));
    }

    Ok(RcptResult {
        ok: true,
        identity_m: Some(m_iss),
        value: Some(o.v),
        reason: String::new(),
    })
}

/// A non-deniable receipt for a direct Identity-bound EOA transfer.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApproveReceipt {
    pub sender: W256,
    pub spender: W256,
    pub chainid: W256,
    pub registry: W256,
    pub e_for_spender: Ctw,
    pub cp_proof: CpProof,
    pub m_named: G1w,
    pub vd_proof: VdProof,
}

/// Verify an EOA approve receipt, naming the sender's registered
/// Identity.  On success `value` is `None` (the amount is the separate
/// ERC-20 Transfer log).
pub fn approve_receipt_verify(
    receipt: &ApproveReceipt,
    registry: &BTreeMap<W256, RegisteredIdentity>,
) -> Result<RcptResult> {
    let Some(snd) = registry.get(&receipt.sender) else {
        return Ok(RcptResult::fail("(c) sender not registered"));
    };
    let Some(e_addr) = &snd.e_addr else {
        return Ok(RcptResult::fail("(c) sender not registered"));
    };
    let Some(spn) = registry.get(&receipt.spender) else {
        return Ok(RcptResult::fail("(c) spender not registered"));
    };

    // Soundness: the re-encryption is of the *registered* credential.
    if !chaum_pedersen_verify(
        e_addr,
        &receipt.e_for_spender,
        &snd.pk,
        &spn.pk,
        &receipt.cp_proof,
        &receipt.sender,
        &receipt.spender,
        &receipt.chainid,
        &receipt.registry,
    )? {
        return Ok(RcptResult::fail("(soundness) approve handshake fails"));
    }

    // Recovery + provability: M_named is the decryption, publicly checked.
    if !verifiable_decrypt_verify(
        &receipt.e_for_spender,
        &spn.pk,
        &receipt.m_named,
        &receipt.vd_proof,
        &receipt.spender,
        &receipt.chainid,
    )? {
        return Ok(RcptResult::fail("(recovery) verifiable decryption fails"));
    }

    Ok(RcptResult {
        ok: true,
        identity_m: Some(receipt.m_named),
        value: None,
        reason: String::new(),
    })
}
