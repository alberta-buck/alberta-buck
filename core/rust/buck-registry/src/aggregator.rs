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

use std::collections::{HashMap, VecDeque};

use crate::tree::{fold_path, IdentityMerkleTree, MembershipProof};
use buck_identity::{IdError, Result, W256, ZERO_W};

/// Root records the ring retains: ten days at an hourly posting, which covers
/// the longest maximum age a consumer declares (accumulator specification,
/// section 5).
pub const ROOT_RING_SIZE: usize = 256;

/// A posted aggregator root, with the time it was posted.  A count of
/// postings is not a bound in time, so each consumer bounds the age instead.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RootRecord {
    pub root: W256,
    pub sequence: u64,
    pub posted_at: f64,
}

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

    /// The one path a circuit folds: the subtree path, then the aggregator's.
    /// The leaf index is the leaf's position in that combined tree.
    pub fn composed(&self) -> MembershipProof {
        let (sub, agg) = (&self.sub_tree_proof, &self.aggregator_proof);
        let mut siblings = sub.siblings.clone();
        siblings.extend_from_slice(&agg.siblings);
        let mut index_bits = sub.index_bits.clone();
        index_bits.extend_from_slice(&agg.index_bits);
        MembershipProof {
            leaf: sub.leaf,
            siblings,
            index_bits,
            root: agg.aggregator_root,
            leaf_index: (agg.aggregator_leaf_index << sub.siblings.len()) | sub.leaf_index,
        }
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
    ring: VecDeque<RootRecord>,
    index: HashMap<W256, RootRecord>,
    sequence: u64,
}

impl CentralMerkleService {
    /// Depth 10 supports up to 1024 sub-trees (the Python default).
    pub fn new(depth: usize) -> Result<Self> {
        Ok(CentralMerkleService {
            tree: IdentityMerkleTree::new(depth)?,
            subs: Vec::new(),
            ring: VecDeque::new(),
            index: HashMap::new(),
            sequence: 0,
        })
    }

    /// Post the current aggregator root at `timestamp`.  The posting is what a
    /// membership proof is checked against, and its age is what a consumer
    /// bounds.
    pub fn post(&mut self, timestamp: f64) -> Result<RootRecord> {
        let rec = RootRecord { root: self.identity_root()?, sequence: self.sequence, posted_at: timestamp };
        self.sequence += 1;
        self.ring.push_back(rec);
        if self.ring.len() > ROOT_RING_SIZE {
            let evicted = self.ring.pop_front().unwrap();
            if self.index.get(&evicted.root).map(|r| r.sequence) == Some(evicted.sequence) {
                self.index.remove(&evicted.root);
            }
        }
        self.index.insert(rec.root, rec);
        Ok(rec)
    }

    /// The record of a retained root, or `None` if never posted or evicted.
    pub fn root_record(&self, root: &W256) -> Option<&RootRecord> {
        self.index.get(root)
    }

    /// Whether a consumer with this maximum age accepts a proof against
    /// `root` at `now`: a nonzero root the ring retains, no older than that.
    pub fn accepts(&self, root: &W256, max_age: f64, now: f64) -> bool {
        if *root == ZERO_W {
            return false;
        }
        self.index.get(root).is_some_and(|r| now - r.posted_at <= max_age)
    }

    /// Age of the oldest retained record: the longest maximum age the ring
    /// honours at the current posting rate.
    pub fn max_retained_age(&self, now: f64) -> f64 {
        self.ring.front().map_or(0.0, |r| now - r.posted_at)
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
