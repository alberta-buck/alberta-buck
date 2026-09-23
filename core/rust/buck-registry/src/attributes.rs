//! Holder-produced attribute proofs -- mirrors `alberta_buck/wallet/attributes.py`.
//!
//! A feature check is a proof the holder produces, not a lookup (accumulator
//! specification, section 10): one path per claimed subtree, all against one
//! posted root.  A verifier MUST name the subtrees it requires and MUST
//! declare its maximum root age, since a proof means nothing without the
//! identifier it is against and the staleness it was accepted under.

use crate::aggregator::{CentralMerkleService, ComposedMembershipProof};
use crate::tree::MembershipProof;
use buck_identity::{IdError, Result, W256};

/// One or more subtree memberships, against a single posted root.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttributeProof {
    pub composed: ComposedMembershipProof,
    pub tree_ids: Vec<String>,
    pub root: W256,
}

impl AttributeProof {
    /// Whether every path is internally valid and shares one root.
    pub fn verify_paths(&self) -> Result<bool> {
        self.composed.verify()
    }
}

/// Compose a holder's subtree memberships into one attribute proof, against
/// the aggregator's current root.
pub fn prove_attributes(
    service: &CentralMerkleService,
    claims: Vec<(String, MembershipProof)>,
    m_x: W256,
    m_y: W256,
) -> Result<AttributeProof> {
    if claims.is_empty() {
        return Err(IdError("an attribute proof needs at least one claim"));
    }
    let tree_ids = claims.iter().map(|(id, _)| id.clone()).collect();
    let composed = service.composed_proof(claims, m_x, m_y)?;
    let root = composed.identity_root();
    Ok(AttributeProof { composed, tree_ids, root })
}

/// Check an attribute proof the way a consumer must: every required subtree
/// claimed, every path valid, one shared root, and that root posted within
/// `max_age` of `now`.
pub fn verify_attributes(
    service: &CentralMerkleService,
    proof: &AttributeProof,
    required: &[&str],
    max_age: f64,
    now: f64,
) -> Result<bool> {
    if required.is_empty() {
        return Err(IdError("a verifier must name the subtrees it requires"));
    }
    if !required.iter().all(|r| proof.tree_ids.iter().any(|t| t == r)) {
        return Ok(false);
    }
    if !proof.verify_paths()? || proof.composed.identity_root() != proof.root {
        return Ok(false);
    }
    Ok(service.accepts(&proof.root, max_age, now))
}
