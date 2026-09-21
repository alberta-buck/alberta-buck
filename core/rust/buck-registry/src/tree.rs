//! Identity Merkle tree -- Poseidon accumulator of registered identity
//! points; mirrors `alberta_buck/registry/tree.py`.
//!
//! Each leaf is `identity_leaf(M) = Poseidon([M.x, M.y] % F_R)`, matching
//! `circuits/identity_membership.circom` byte-for-byte.  Roots and paths
//! are recomputed from the leaf list by explicit layer folding -- exactly
//! the Python `_compute_root` / `_derive_path` algorithms -- so leaf
//! REPLACEMENT (the aggregator's `update_sub_root`, the feature
//! authority's `revoke`) is as well-defined here as there.

use buck_identity::notes;
use buck_identity::poseidon::poseidon;
use buck_identity::{G1w, IdError, Result, W256, ZERO_W};

/// `identity_leaf(M) = Poseidon([M.x, M.y])` -- re-exported from the
/// identity kernel (the same function the circuits pin).
pub fn identity_leaf(m_point: &G1w) -> Result<W256> {
    notes::identity_leaf(m_point)
}

/// `identity_leaf_salted(M, salt) = Poseidon([M.x, M.y, salt])` -- the leaf
/// of a private subtree, re-exported from the identity kernel.
pub fn identity_leaf_salted(m_point: &G1w, salt: &W256) -> Result<W256> {
    notes::identity_leaf_salted(m_point, salt)
}

/// `receiving_leaf(m_rec, k_recv, salt) = Poseidon([m_rec, k_recv, salt])`
/// -- the leaf of a private identity-registry subtree, binding an Identity
/// to the receiving key its Notes are addressed to.  It commits the scalars,
/// not the points; see the kernel function for why.
pub fn receiving_leaf(m_rec: &W256, k_recv: &W256, salt: &W256) -> Result<W256> {
    notes::receiving_leaf(m_rec, k_recv, salt)
}

/// `mailbox_leaf(M, pk_recv, salt)` -- the payer's view of the same
/// association, over the POINTS, so it needs no secret to check.
pub fn mailbox_leaf(m_point: &G1w, pk_recv: &G1w, salt: &W256) -> Result<W256> {
    notes::mailbox_leaf(m_point, pk_recv, salt)
}

/// Sentinel for an empty leaf (depth-0 zero).
pub const EMPTY_LEAF: W256 = ZERO_W;

/// A Merkle proof that a specific leaf is in the tree under a given root.
///
/// `siblings[d]` is the sibling hash at depth `d`; `index_bits[d]` is 0 if
/// the leaf was on the left at that level, 1 if on the right.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MembershipProof {
    pub leaf: W256,
    pub siblings: Vec<W256>,
    pub index_bits: Vec<u8>,
    pub root: W256,
    pub leaf_index: usize,
}

impl MembershipProof {
    /// Recompute the path and check against `root`.
    pub fn verify(&self) -> Result<bool> {
        Ok(fold_path(&self.leaf, &self.siblings, &self.index_bits)? == self.root)
    }
}

/// Fold a leaf up a sibling path: `bit == 0` keeps the running node on the
/// left.  The shared verification kernel of every membership proof here.
pub fn fold_path(leaf: &W256, siblings: &[W256], index_bits: &[u8]) -> Result<W256> {
    let mut cur = *leaf;
    for (sib, bit) in siblings.iter().zip(index_bits.iter()) {
        cur = if *bit == 0 {
            poseidon(&[cur, *sib])?
        } else {
            poseidon(&[*sib, cur])?
        };
    }
    Ok(cur)
}

/// Incremental Poseidon Merkle tree of identity leaves.
///
/// Depth `d` supports up to `2^d` leaves; insertion is sequential.  The
/// Python reference keeps a Tornado-style `filled_subtrees` cache, but its
/// root and paths are always derived from the full leaf list -- this port
/// keeps only the leaf list (the cache was write-only there).
#[derive(Debug, Clone)]
pub struct IdentityMerkleTree {
    depth: usize,
    leaves: Vec<W256>,
    zeros: Vec<W256>,
}

impl IdentityMerkleTree {
    /// `depth` must lie in `[1, 32]` (Python raises the same bound).
    pub fn new(depth: usize) -> Result<Self> {
        if !(1..=32).contains(&depth) {
            return Err(IdError("depth must be in [1, 32]"));
        }
        let mut zeros = vec![EMPTY_LEAF; depth + 1];
        for d in 1..=depth {
            zeros[d] = poseidon(&[zeros[d - 1], zeros[d - 1]])?;
        }
        Ok(IdentityMerkleTree {
            depth,
            leaves: Vec::new(),
            zeros,
        })
    }

    pub fn depth(&self) -> usize {
        self.depth
    }

    pub fn leaves(&self) -> &[W256] {
        &self.leaves
    }

    /// Number of leaves inserted.
    pub fn count(&self) -> usize {
        self.leaves.len()
    }

    /// Index the next insertion will occupy.
    pub fn next_index(&self) -> usize {
        self.leaves.len()
    }

    /// `zeros[d]` = root of an all-empty subtree of height `d`.
    pub fn zero(&self, d: usize) -> W256 {
        self.zeros[d]
    }

    /// Current Merkle root (empty tree -> `zeros[depth]`).
    pub fn root(&self) -> Result<W256> {
        if self.leaves.is_empty() {
            return Ok(self.zeros[self.depth]);
        }
        let mut nodes = self.leaves.clone();
        for d in 0..self.depth {
            let mut next = Vec::with_capacity(nodes.len().div_ceil(2));
            for pair in nodes.chunks(2) {
                let left = pair[0];
                let right = if pair.len() == 2 { pair[1] } else { self.zeros[d] };
                next.push(poseidon(&[left, right])?);
            }
            nodes = next;
        }
        Ok(nodes[0])
    }

    /// Insert a pre-computed identity leaf; returns its index.
    pub fn insert_leaf(&mut self, leaf: W256) -> usize {
        let idx = self.leaves.len();
        self.leaves.push(leaf);
        idx
    }

    /// Insert an identity point.
    pub fn insert_identity(&mut self, m_point: &G1w) -> Result<usize> {
        Ok(self.insert_leaf(identity_leaf(m_point)?))
    }

    /// Insert multiple leaves; returns the index of the first.
    pub fn insert_batch(&mut self, leaves: &[W256]) -> usize {
        let first = self.leaves.len();
        self.leaves.extend_from_slice(leaves);
        first
    }

    /// Replace the leaf at `index` (aggregator sub-root updates, feature
    /// revocation).  Mirrors the Python direct `leaves[idx] = ...` write.
    pub fn set_leaf(&mut self, index: usize, leaf: W256) -> Result<()> {
        if index >= self.leaves.len() {
            return Err(IdError("leaf index out of range"));
        }
        self.leaves[index] = leaf;
        Ok(())
    }

    /// The Merkle authentication path for the leaf at `index`.
    ///
    /// Builds the full layer structure from all leaves (accounting for
    /// leaves inserted after `index`), exactly as the Python
    /// `_derive_path` does.
    pub fn path(&self, index: usize) -> Result<MembershipProof> {
        if index >= self.leaves.len() {
            return Err(IdError("leaf index out of range"));
        }
        let leaf = self.leaves[index];
        let root = self.root()?;

        let mut layers: Vec<Vec<W256>> = vec![self.leaves.clone()];
        for d in 0..self.depth {
            let nodes = &layers[d];
            let mut next = Vec::with_capacity(nodes.len().div_ceil(2));
            for pair in nodes.chunks(2) {
                let left = pair[0];
                let right = if pair.len() == 2 { pair[1] } else { self.zeros[d] };
                next.push(poseidon(&[left, right])?);
            }
            layers.push(next);
        }

        let mut siblings = Vec::with_capacity(self.depth);
        let mut bits = Vec::with_capacity(self.depth);
        let mut pos = index;
        for (d, layer) in layers.iter().take(self.depth).enumerate() {
            let bit = (pos & 1) as u8;
            bits.push(bit);
            let sib_pos = pos ^ 1;
            siblings.push(if sib_pos < layer.len() {
                layer[sib_pos]
            } else {
                self.zeros[d]
            });
            pos >>= 1;
        }

        Ok(MembershipProof {
            leaf,
            siblings,
            index_bits: bits,
            root,
            leaf_index: index,
        })
    }

    /// True if `leaf` has been inserted.
    pub fn contains(&self, leaf: &W256) -> bool {
        self.leaves.contains(leaf)
    }

    /// Position of `leaf` in the tree.
    pub fn index_of_leaf(&self, leaf: &W256) -> Result<usize> {
        self.leaves
            .iter()
            .position(|l| l == leaf)
            .ok_or(IdError("leaf is not in the tree"))
    }

    /// Position of an identity point's leaf in the tree.
    pub fn index_of_identity(&self, m_point: &G1w) -> Result<usize> {
        self.index_of_leaf(&identity_leaf(m_point)?)
    }

    /// True iff identity point `M` is in the tree; with `root` given,
    /// additionally require a membership path folding to that root --
    /// mirroring `unilateral_a2.IdentityTree.contains`.
    pub fn contains_identity(&self, m_point: &G1w, root: Option<&W256>) -> Result<bool> {
        let leaf = identity_leaf(m_point)?;
        let Ok(idx) = self.index_of_leaf(&leaf) else {
            return Ok(false);
        };
        match root {
            None => Ok(true),
            Some(r) => {
                let proof = self.path(idx)?;
                Ok(proof.verify()? && proof.root == *r)
            }
        }
    }

    /// Reconstruct a tree from a leaf list (event-log replay).
    pub fn from_leaves(leaves: &[W256], depth: usize) -> Result<Self> {
        let mut tree = Self::new(depth)?;
        tree.insert_batch(leaves);
        Ok(tree)
    }
}

/// The on-chain identity-tree depth: `IdentityRegistry.IDENTITY_TREE_DEPTH`
/// and the membership circuits both fix 10 (`unilateral_a2.IdentityTree`).
///
/// This is the AGGREGATOR depth -- the tree whose root IS `identityRoot`.
/// It is the only one of the three that is protocol-critical, because the
/// circuits prove a path to exactly this root.
pub const IDENTITY_TREE_DEPTH: usize = 20;

/// Alias making the role explicit at call sites that compose sub-trees.
pub const AGGREGATOR_DEPTH: usize = IDENTITY_TREE_DEPTH;

/// One registry organization's own identity sub-tree.  Deliberately deeper
/// than the aggregator: it sizes how many identities a single organization
/// can hold (2**12 ~ 4K), which is a capacity question rather than a protocol
/// one.  Mirrors `alberta_buck.registry.tree.KYC_SUBTREE_DEPTH`, and the
/// committed vectors pin it (`reg_a.depth` / `reg_b.depth`).
pub const KYC_SUBTREE_DEPTH: usize = 12;

/// A feature authority's sub-tree (attributes such as licences).  Equal to
/// AGGREGATOR_DEPTH today by coincidence of capacity, not by requirement --
/// named separately so raising one cannot silently raise the other.
pub const FEATURE_SUBTREE_DEPTH: usize = 10;

/// The wallet-facing point-centric tree at the on-chain depth.
pub fn identity_tree() -> IdentityMerkleTree {
    IdentityMerkleTree::new(IDENTITY_TREE_DEPTH).expect("depth 10 is valid")
}
