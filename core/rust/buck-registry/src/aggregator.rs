//! Central Merkle Service -- aggregates registry and feature sub-tree
//! roots into the single on-chain `identityRoot`; mirrors
//! `alberta_buck/registry/merkle_service.py`.
//!
//! Each sub-tree (KYC registry or feature authority) enrolls once for a
//! leaf slot in the aggregator tree and thereafter pushes root updates.
//! A full membership proof chains a sub-tree path with an aggregator
//! path; AND-composition uses several full proofs sharing one root.
//!
//! Timestamps are explicit `f64` arguments (the Python shim passes
//! `time.time()`); the kernel holds no clock.

use crate::tree::{fold_path, IdentityMerkleTree, MembershipProof};
use buck_identity::{IdError, Result, W256};

/// Sub-tree kind labels (`SubTreeKind` in the Python reference).
pub const KIND_KYC: &str = "kyc";
pub const KIND_FEATURE: &str = "feature";

/// A sub-tree enrolled in the central service.
#[derive(Debug, Clone, PartialEq)]
pub struct SubTreeRecord {
    pub sub_tree_id: String,
    pub kind: String,
    pub sub_root: W256,
    pub updated_at: f64,
    pub aggregator_leaf_index: usize,
}

/// Proof that a sub-root is in the aggregator tree.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AggregatorMembershipProof {
    pub sub_root: W256,
    pub siblings: Vec<W256>,
    pub index_bits: Vec<u8>,
    pub aggregator_root: W256,
    pub sub_tree_id: String,
    pub aggregator_leaf_index: usize,
}

impl AggregatorMembershipProof {
    pub fn verify(&self) -> Result<bool> {
        Ok(fold_path(&self.sub_root, &self.siblings, &self.index_bits)? == self.aggregator_root)
    }
}

/// A complete identity membership proof: sub-tree path + aggregator path.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FullMembershipProof {
    pub sub_tree_proof: MembershipProof,
    pub aggregator_proof: AggregatorMembershipProof,
    pub m_x: W256,
    pub m_y: W256,
}

impl FullMembershipProof {
    pub fn identity_root(&self) -> W256 {
        self.aggregator_proof.aggregator_root
    }

    /// Both paths valid and consistent (sub root == aggregator leaf).
    pub fn verify(&self) -> Result<bool> {
        if !self.sub_tree_proof.verify()? {
            return Ok(false);
        }
        if !self.aggregator_proof.verify()? {
            return Ok(false);
        }
        Ok(self.sub_tree_proof.root == self.aggregator_proof.sub_root)
    }
}

/// AND-composed proofs: every half valid, all sharing one identityRoot.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ComposedMembershipProof {
    pub proofs: Vec<FullMembershipProof>,
}

impl ComposedMembershipProof {
    pub fn identity_root(&self) -> W256 {
        self.proofs
            .first()
            .map(|p| p.identity_root())
            .unwrap_or([0u8; 32])
    }

    pub fn verify(&self) -> Result<bool> {
        if self.proofs.is_empty() {
            return Ok(false);
        }
        let root = self.identity_root();
        for p in &self.proofs {
            if !p.verify()? || p.identity_root() != root {
                return Ok(false);
            }
        }
        Ok(true)
    }
}

/// The central aggregator: a Merkle tree whose leaves are sub-roots.
#[derive(Debug, Clone)]
pub struct CentralMerkleService {
    tree: IdentityMerkleTree,
    subs: Vec<SubTreeRecord>,
}

impl CentralMerkleService {
    /// Depth 10 supports up to 1024 sub-trees (the Python default).
    pub fn new(depth: usize) -> Result<Self> {
        Ok(CentralMerkleService {
            tree: IdentityMerkleTree::new(depth)?,
            subs: Vec::new(),
        })
    }

    /// The current aggregator root (= on-chain identityRoot).
    pub fn identity_root(&self) -> Result<W256> {
        self.tree.root()
    }

    pub fn sub_tree_count(&self) -> usize {
        self.subs.len()
    }

    /// Enrolled sub-trees in insertion order, optionally kind-filtered.
    pub fn list_sub_trees(&self, kind: Option<&str>) -> Vec<&SubTreeRecord> {
        self.subs
            .iter()
            .filter(|r| kind.is_none_or(|k| r.kind == k))
            .collect()
    }

    pub fn get_sub_tree(&self, sub_tree_id: &str) -> Option<&SubTreeRecord> {
        self.subs.iter().find(|r| r.sub_tree_id == sub_tree_id)
    }

    /// Enroll a new sub-tree, allocating its aggregator leaf slot.
    pub fn enroll(
        &mut self,
        sub_tree_id: &str,
        kind: &str,
        initial_sub_root: W256,
        timestamp: f64,
    ) -> Result<&SubTreeRecord> {
        if self.get_sub_tree(sub_tree_id).is_some() {
            return Err(IdError("sub-tree already enrolled"));
        }
        if kind != KIND_KYC && kind != KIND_FEATURE {
            return Err(IdError("unknown sub-tree kind"));
        }
        let idx = self.tree.insert_leaf(initial_sub_root);
        self.subs.push(SubTreeRecord {
            sub_tree_id: sub_tree_id.to_string(),
            kind: kind.to_string(),
            sub_root: initial_sub_root,
            updated_at: timestamp,
            aggregator_leaf_index: idx,
        });
        Ok(self.subs.last().unwrap())
    }

    /// Replace a sub-tree's aggregator leaf; returns the new root.
    pub fn update_sub_root(
        &mut self,
        sub_tree_id: &str,
        new_sub_root: W256,
        timestamp: f64,
    ) -> Result<W256> {
        let rec = self
            .subs
            .iter_mut()
            .find(|r| r.sub_tree_id == sub_tree_id)
            .ok_or(IdError("unknown sub-tree"))?;
        let idx = rec.aggregator_leaf_index;
        rec.sub_root = new_sub_root;
        rec.updated_at = timestamp;
        self.tree.set_leaf(idx, new_sub_root)?;
        self.tree.root()
    }

    /// Apply several updates, then return the new aggregator root -- the
    /// transaction the service posts on chain.
    pub fn batch_update_and_root(
        &mut self,
        updates: &[(String, W256)],
        timestamp: f64,
    ) -> Result<W256> {
        for (id, root) in updates {
            self.update_sub_root(id, *root, timestamp)?;
        }
        self.identity_root()
    }

    /// Proof that a sub-tree's current root is in the aggregator tree.
    pub fn aggregator_proof(&self, sub_tree_id: &str) -> Result<AggregatorMembershipProof> {
        let rec = self
            .get_sub_tree(sub_tree_id)
            .ok_or(IdError("unknown sub-tree"))?;
        let mp = self.tree.path(rec.aggregator_leaf_index)?;
        Ok(AggregatorMembershipProof {
            sub_root: rec.sub_root,
            siblings: mp.siblings,
            index_bits: mp.index_bits,
            aggregator_root: mp.root,
            sub_tree_id: rec.sub_tree_id.clone(),
            aggregator_leaf_index: rec.aggregator_leaf_index,
        })
    }

    /// Combine a sub-tree proof with this service's aggregator proof.
    pub fn full_proof(
        &self,
        sub_tree_id: &str,
        sub_tree_proof: MembershipProof,
        m_x: W256,
        m_y: W256,
    ) -> Result<FullMembershipProof> {
        let rec = self
            .get_sub_tree(sub_tree_id)
            .ok_or(IdError("unknown sub-tree"))?;
        if sub_tree_proof.root != rec.sub_root {
            return Err(IdError("sub_tree root does not match recorded sub_root"));
        }
        Ok(FullMembershipProof {
            sub_tree_proof,
            aggregator_proof: self.aggregator_proof(sub_tree_id)?,
            m_x,
            m_y,
        })
    }

    /// AND-composed proof across several sub-trees.
    pub fn composed_proof(
        &self,
        sub_tree_proofs: Vec<(String, MembershipProof)>,
        m_x: W256,
        m_y: W256,
    ) -> Result<ComposedMembershipProof> {
        let mut proofs = Vec::with_capacity(sub_tree_proofs.len());
        for (id, mp) in sub_tree_proofs {
            proofs.push(self.full_proof(&id, mp, m_x, m_y)?);
        }
        Ok(ComposedMembershipProof { proofs })
    }
}
