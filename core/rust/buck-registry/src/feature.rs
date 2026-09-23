//! Feature Authority -- certifies that identity points possess specific
//! attributes; mirrors `alberta_buck/registry/feature_authority.py`.
//!
//! Maintains a Poseidon Merkle tree of qualified identities; the sub-root goes
//! to the central aggregator like any registry's, so a holder can AND-compose
//! "registered AND has feature X" proofs against the single on-chain root.
//!
//! A subtree is PRIVATE or PUBLIC, never per member (accumulator
//! specification, section 4).  A private subtree's leaf is the hiding
//! commitment `identity_leaf_salted(M, salt)`, under the holder's own salt, so
//! the authority keeps the current association -- identity, salt, leaf index --
//! in order to clear it, and no history: a superseded salt is discarded on
//! re-association (section 8.3).  A public subtree's leaf is `identity_leaf(M)`.

use crate::tree::{identity_leaf, identity_leaf_salted, IdentityMerkleTree, MembershipProof, EMPTY_LEAF};
use buck_identity::{G1w, IdError, Result, W256};

/// A single identity-feature attestation.
#[derive(Debug, Clone, PartialEq)]
pub struct FeatureRecord {
    pub m_point: G1w,
    pub leaf: W256,
    pub leaf_index: usize,
    pub salt: Option<W256>,
    pub attested_at: f64,
    pub evidence_hash: Option<[u8; 32]>,
}

/// An attribute certifier maintaining a tree of qualified identities.
#[derive(Debug, Clone)]
pub struct FeatureAuthority {
    pub feature_id: String,
    pub private: bool,
    tree: IdentityMerkleTree,
    records: Vec<FeatureRecord>,
}

impl FeatureAuthority {
    /// `feature_id` must use the `feature:` prefix convention.
    pub fn new(feature_id: &str, tree_depth: usize, private: bool) -> Result<Self> {
        if !feature_id.starts_with("feature:") {
            return Err(IdError(
                "feature_id must use the 'feature:' prefix convention",
            ));
        }
        Ok(FeatureAuthority {
            feature_id: feature_id.to_string(),
            private,
            tree: IdentityMerkleTree::new(tree_depth)?,
            records: Vec::new(),
        })
    }

    /// Current root of the feature tree.
    pub fn sub_root(&self) -> Result<W256> {
        self.tree.root()
    }

    /// Number of leaves inserted (cleared leaves included, as in Python).
    pub fn identity_count(&self) -> usize {
        self.tree.count()
    }

    /// The leaf this subtree's class requires: the salted commitment for a
    /// private subtree (salt mandatory), the unsalted leaf for a public one
    /// (salt refused).
    fn leaf_for(&self, m_point: &G1w, salt: Option<&W256>) -> Result<W256> {
        match (self.private, salt) {
            (true, Some(s)) => identity_leaf_salted(m_point, s),
            (true, None) => Err(IdError("a private subtree's attestation needs the holder's salt")),
            (false, None) => identity_leaf(m_point),
            (false, Some(_)) => Err(IdError("a public subtree takes no salt")),
        }
    }

    /// Attest that identity point `M` possesses this feature; the caller
    /// supplies `attested_at` (no clock in the kernel).  Duplicate
    /// attestations are rejected.
    pub fn attest(
        &mut self,
        m_point: &G1w,
        salt: Option<&W256>,
        attested_at: f64,
        evidence_hash: Option<[u8; 32]>,
    ) -> Result<&FeatureRecord> {
        let leaf = self.leaf_for(m_point, salt)?;
        if self.tree.contains(&leaf) {
            return Err(IdError("identity already attested for feature"));
        }
        let leaf_index = self.tree.insert_leaf(leaf);
        self.records.retain(|r| r.m_point != *m_point);
        self.records.push(FeatureRecord {
            m_point: *m_point,
            leaf,
            leaf_index,
            salt: salt.copied(),
            attested_at,
            evidence_hash,
        });
        Ok(self.records.last().unwrap())
    }

    /// Revoke by clearing the leaf to the empty sentinel; returns the cleared
    /// index, or `None` if `M` was not attested.  The association, salt
    /// included, is discarded with it.
    pub fn revoke(&mut self, m_point: &G1w) -> Result<Option<usize>> {
        let idx = match self.records.iter().position(|r| r.m_point == *m_point) {
            Some(i) => self.records.remove(i).leaf_index,
            None => {
                if self.private {
                    return Ok(None);
                }
                let Ok(idx) = self.tree.index_of_leaf(&identity_leaf(m_point)?) else {
                    return Ok(None);
                };
                idx
            }
        };
        self.tree.set_leaf(idx, EMPTY_LEAF)?;
        Ok(Some(idx))
    }

    fn index_of_identity(&self, m_point: &G1w) -> Result<Option<usize>> {
        if let Some(r) = self.records.iter().find(|r| r.m_point == *m_point) {
            return Ok(Some(r.leaf_index));
        }
        if self.private {
            return Ok(None);
        }
        Ok(self.tree.index_of_leaf(&identity_leaf(m_point)?).ok())
    }

    /// True if `M` is currently attested for this feature.
    pub fn has_identity(&self, m_point: &G1w) -> Result<bool> {
        Ok(self.index_of_identity(m_point)?.is_some())
    }

    /// Merkle proof for the identity at `leaf_index`.
    pub fn membership_proof(&self, leaf_index: usize) -> Result<MembershipProof> {
        self.tree.path(leaf_index)
    }

    /// Merkle proof for identity point `M` (None if not attested).
    pub fn membership_proof_for_identity(&self, m_point: &G1w) -> Result<Option<MembershipProof>> {
        match self.index_of_identity(m_point)? {
            Some(idx) => Ok(Some(self.tree.path(idx)?)),
            None => Ok(None),
        }
    }

    pub fn get_record(&self, leaf_index: usize) -> Option<&FeatureRecord> {
        self.records.iter().find(|r| r.leaf_index == leaf_index)
    }
}
