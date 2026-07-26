//! Feature Authority -- certifies that identity points possess specific
//! attributes; mirrors `alberta_buck/registry/feature_authority.py`.
//!
//! Maintains a Poseidon Merkle tree of qualified identity points; the
//! sub-root goes to the central aggregator like any KYC registry's, so an
//! identity holder can AND-compose "registered AND has feature X" proofs
//! against the single on-chain root.

use crate::tree::{identity_leaf, IdentityMerkleTree, MembershipProof, EMPTY_LEAF};
use buck_identity::{G1w, IdError, Result, W256};

/// A single identity-feature attestation.
#[derive(Debug, Clone, PartialEq)]
pub struct FeatureRecord {
    pub m_point: G1w,
    pub leaf: W256,
    pub leaf_index: usize,
    pub attested_at: f64,
    pub evidence_hash: Option<[u8; 32]>,
}

/// An attribute certifier maintaining a tree of qualified identities.
#[derive(Debug, Clone)]
pub struct FeatureAuthority {
    pub feature_id: String,
    tree: IdentityMerkleTree,
    records: Vec<FeatureRecord>,
}

impl FeatureAuthority {
    /// `feature_id` must use the `feature:` prefix convention.
    pub fn new(feature_id: &str, tree_depth: usize) -> Result<Self> {
        if !feature_id.starts_with("feature:") {
            return Err(IdError(
                "feature_id must use the 'feature:' prefix convention",
            ));
        }
        Ok(FeatureAuthority {
            feature_id: feature_id.to_string(),
            tree: IdentityMerkleTree::new(tree_depth)?,
            records: Vec::new(),
        })
    }

    /// Current root of the feature tree.
    pub fn sub_root(&self) -> Result<W256> {
        self.tree.root()
    }

    /// Number of identities attested.
    pub fn identity_count(&self) -> usize {
        self.tree.count()
    }

    /// Attest that identity point `M` possesses this feature; the caller
    /// supplies `attested_at` (no clock in the kernel).  Duplicate
    /// attestations are rejected.
    pub fn attest(
        &mut self,
        m_point: &G1w,
        attested_at: f64,
        evidence_hash: Option<[u8; 32]>,
    ) -> Result<&FeatureRecord> {
        let leaf = identity_leaf(m_point)?;
        if self.tree.contains(&leaf) {
            return Err(IdError("identity already attested for feature"));
        }
        let leaf_index = self.tree.insert_leaf(leaf);
        self.records.push(FeatureRecord {
            m_point: *m_point,
            leaf,
            leaf_index,
            attested_at,
            evidence_hash,
        });
        Ok(self.records.last().unwrap())
    }

    /// Revoke by clearing the leaf to the empty sentinel; returns the
    /// cleared index, or `None` if `M` was not attested.
    pub fn revoke(&mut self, m_point: &G1w) -> Result<Option<usize>> {
        let leaf = identity_leaf(m_point)?;
        let Ok(idx) = self.tree.index_of_leaf(&leaf) else {
            return Ok(None);
        };
        self.tree.set_leaf(idx, EMPTY_LEAF)?;
        Ok(Some(idx))
    }

    /// True if `M` is attested for this feature.
    pub fn has_identity(&self, m_point: &G1w) -> Result<bool> {
        Ok(self.tree.contains(&identity_leaf(m_point)?))
    }

    /// Merkle proof for the identity at `leaf_index`.
    pub fn membership_proof(&self, leaf_index: usize) -> Result<MembershipProof> {
        self.tree.path(leaf_index)
    }

    /// Merkle proof for identity point `M` (None if not attested).
    pub fn membership_proof_for_identity(&self, m_point: &G1w) -> Result<Option<MembershipProof>> {
        let leaf = identity_leaf(m_point)?;
        let Ok(idx) = self.tree.index_of_leaf(&leaf) else {
            return Ok(None);
        };
        Ok(Some(self.tree.path(idx)?))
    }

    pub fn get_record(&self, leaf_index: usize) -> Option<&FeatureRecord> {
        self.records.iter().find(|r| r.leaf_index == leaf_index)
    }
}
