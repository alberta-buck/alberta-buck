"""Central Merkle Service -- aggregates registry and feature sub-tree roots.

Each Registry (provincial/state/federal) maintains its own IdentityMerkleTree of
verified identity points.  Each Feature Authority maintains a tree of identity
points possessing a specific attribute (age>18, has-license, region).  An identity
M can appear in many feature trees -- every feature it qualifies for.

The CentralMerkleService takes every sub-root (registry and feature alike) and
builds a second-level Poseidon Merkle tree whose leaves are these sub-roots.
The root of *this* tree -- the aggregator root -- is what goes on chain as
IdentityRegistry.identityRoot.  This is the single O(1) on-chain storage slot,
regardless of how many registries or features exist.

A full membership proof for an identity point M has two parts:
1. A sub-tree proof: identity_leaf(M) -> sub_root (in the feature/registry tree)
2. An aggregator proof: sub_root -> identityRoot (in the aggregator tree)

For AND-composed proofs (M is registered AND M has feature X), the prover
produces two sub-tree proofs, each with its own aggregator path.  Since both
aggregator paths verify against the same identityRoot, the verifier checks
both paths independently and confirms they reach the same root.

This federates identity attributes: registries and feature authorities operate
independently.  The central service aggregates periodically; only the final root
touches the chain.  An identity is usable only after its sub-root is committed
to the aggregator and the aggregator root is posted on chain (the "commit-before-use"
discipline from the identity-axis design (now in alberta-buck-notes.org "Mutual Decryptability" and notes-flow "commit-before-use").).

Design trade-off vs. N independent roots on-chain:

| Model               | On-chain storage | Update gas           | Privacy (feature?)    |
|---------------------+------------------+----------------------+-----------------------|
| N independent roots | O(N) slots       | O(N * updates)       | Revealed (which root) |
| Single aggregator   | 1 slot           | O(1) per batch       | Hidden (one root)     |

Reference: alberta-buck-notes.org and alberta-buck-notes-flow.org (Registry-Identity Accumulator, commit-before-use). Section "The Registry-Identity
Accumulator".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from alberta_buck.registry.tree import (
    IdentityMerkleTree,
    MembershipProof,
    identity_leaf,
    EMPTY_LEAF,
)
from alberta_buck.wallet.poseidon import poseidon


# ---------------------------------------------------------------------------
# Sub-tree kinds
# ---------------------------------------------------------------------------

class SubTreeKind:
    """Labels for the kind of sub-tree in the aggregator.

    KYC registries certify that M is a genuine person.  Feature authorities
    certify that M possesses a specific attribute.
    """
    KYC: str = "kyc"
    FEATURE: str = "feature"


# ---------------------------------------------------------------------------
# Sub-tree record
# ---------------------------------------------------------------------------

@dataclass
class SubTreeRecord:
    """A sub-tree enrolled in the central Merkle service.

    Attributes:
        sub_tree_id: Stable identifier (e.g. "ca-bc-2026" or "feature:age-over-18").
        kind: SubTreeKind.KYC or SubTreeKind.FEATURE.
        sub_root: The sub-tree's current Merkle root.
        updated_at: POSIX timestamp of last sub-root update.
        aggregator_leaf_index: Leaf position in the aggregator tree.
    """
    sub_tree_id: str
    kind: str
    sub_root: int
    updated_at: float
    aggregator_leaf_index: int

    @property
    def is_kyc(self) -> bool:
        return self.kind == SubTreeKind.KYC

    @property
    def is_feature(self) -> bool:
        return self.kind == SubTreeKind.FEATURE


# ---------------------------------------------------------------------------
# Aggregator membership proof
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AggregatorMembershipProof:
    """Proof that a sub-root is in the aggregator tree.

    Attributes:
        sub_root: The sub-tree root (leaf in the aggregator tree).
        siblings: Sibling hashes at each aggregator tree depth.
        index_bits: Path direction bits (0=left, 1=right).
        aggregator_root: The combined root (= on-chain identityRoot).
        sub_tree_id: Which sub-tree this proof is for.
        aggregator_leaf_index: Leaf position in the aggregator tree.
    """
    sub_root: int
    siblings: List[int]
    index_bits: List[int]
    aggregator_root: int
    sub_tree_id: str
    aggregator_leaf_index: int

    def verify(self) -> bool:
        """Recompute the path and check against aggregator_root."""
        cur = self.sub_root
        for sib, bit in zip(self.siblings, self.index_bits):
            cur = poseidon([cur, sib]) if bit == 0 else poseidon([sib, cur])
        return cur == self.aggregator_root


@dataclass(frozen=True)
class FullMembershipProof:
    """A complete identity membership proof: sub-tree path + aggregator path.

    This is what a client needs to prove on chain (via the unified membership
    SNARK) that their identity point M is a registered identity or has a feature.

    Attributes:
        sub_tree_proof: Proof that identity_leaf(M) is in the sub-tree.
        aggregator_proof: Proof that the sub-root is in the aggregator tree.
        M_x: The identity point's x coordinate (for the non-native G1 tie).
        M_y: The identity point's y coordinate.
    """
    sub_tree_proof: MembershipProof
    aggregator_proof: AggregatorMembershipProof
    M_x: int
    M_y: int

    @property
    def identity_root(self) -> int:
        return self.aggregator_proof.aggregator_root

    def verify(self) -> bool:
        """Off-chain verification: both paths must be valid and consistent."""
        if not self.sub_tree_proof.verify():
            return False
        if not self.aggregator_proof.verify():
            return False
        return self.sub_tree_proof.root == self.aggregator_proof.sub_root


@dataclass(frozen=True)
class ComposedMembershipProof:
    """AND-composed proofs: M is in sub-tree A AND sub-tree B.

    Used when a spend requires both "M is a registered identity" (KYC) and
    "M has feature X" (e.g. age>18).  Each half has its own sub-tree path
    and aggregator path, but both aggregator paths verify against the same
    identityRoot.

    Attributes:
        proofs: List of FullMembershipProof, one per required sub-tree.
    """
    proofs: List[FullMembershipProof]

    @property
    def identity_root(self) -> int:
        return self.proofs[0].identity_root if self.proofs else 0

    def verify(self) -> bool:
        """All proofs must be individually valid and share the same identityRoot."""
        if not self.proofs:
            return False
        root = self.identity_root
        for p in self.proofs:
            if not p.verify():
                return False
            if p.identity_root != root:
                return False
        return True


# ---------------------------------------------------------------------------
# Central Merkle Service
# ---------------------------------------------------------------------------

class CentralMerkleService:
    """Aggregates sub-roots from registries and feature authorities into a single
    on-chain root.

    Each sub-tree (KYC registry or feature authority) calls enroll() once to
    get a leaf slot in the aggregator tree.  Thereafter it calls update_sub_root()
    whenever its tree grows.  The aggregator re-hashes the path and emits the
    new identity_root.

    Args:
        depth: Aggregator tree depth.  Depth 10 supports up to 1024 sub-trees.
    """

    def __init__(self, depth: int = 10) -> None:
        self._tree = IdentityMerkleTree(depth=depth)
        self._sub_trees: Dict[str, SubTreeRecord] = {}

    # -- properties ----------------------------------------------------------

    @property
    def identity_root(self) -> int:
        """The current aggregator root (= on-chain identityRoot)."""
        return self._tree.root()

    @property
    def sub_tree_count(self) -> int:
        """Number of enrolled sub-trees."""
        return len(self._sub_trees)

    @property
    def sub_tree_ids(self) -> List[str]:
        """Stable identifiers of all enrolled sub-trees."""
        return list(self._sub_trees.keys())

    def list_sub_trees(self, kind: Optional[str] = None) -> List[SubTreeRecord]:
        """List enrolled sub-trees, optionally filtered by kind."""
        recs = list(self._sub_trees.values())
        if kind is not None:
            recs = [r for r in recs if r.kind == kind]
        return recs

    def get_sub_tree(self, sub_tree_id: str) -> Optional[SubTreeRecord]:
        """Look up an enrolled sub-tree by id."""
        return self._sub_trees.get(sub_tree_id)

    # -- enrollment ----------------------------------------------------------

    def enroll(
        self,
        sub_tree_id: str,
        kind: str,
        initial_sub_root: int,
        timestamp: Optional[float] = None,
    ) -> SubTreeRecord:
        """Enroll a new sub-tree, allocating a leaf slot in the aggregator.

        Args:
            sub_tree_id: Stable identifier (e.g. "ca-bc-2026").
            kind: SubTreeKind.KYC or SubTreeKind.FEATURE.
            initial_sub_root: The sub-tree's initial Merkle root.
            timestamp: POSIX timestamp (defaults to time.time()).

        Returns:
            SubTreeRecord with the allocated aggregator leaf index.

        Raises:
            ValueError: If sub_tree_id is already enrolled.
        """
        import time
        if sub_tree_id in self._sub_trees:
            raise ValueError(f"sub-tree already enrolled: {sub_tree_id}")
        if kind not in (SubTreeKind.KYC, SubTreeKind.FEATURE):
            raise ValueError(f"unknown sub-tree kind: {kind}")
        ts = timestamp if timestamp is not None else time.time()
        leaf = initial_sub_root
        idx = self._tree.insert_leaf(leaf)
        rec = SubTreeRecord(
            sub_tree_id=sub_tree_id, kind=kind, sub_root=initial_sub_root,
            updated_at=ts, aggregator_leaf_index=idx,
        )
        self._sub_trees[sub_tree_id] = rec
        return rec

    def enroll_registry(
        self, registry_id: str, initial_sub_root: int,
        timestamp: Optional[float] = None,
    ) -> SubTreeRecord:
        """Enroll a KYC registry sub-tree (convenience)."""
        return self.enroll(registry_id, SubTreeKind.KYC, initial_sub_root, timestamp)

    def enroll_feature(
        self, feature_id: str, initial_sub_root: int,
        timestamp: Optional[float] = None,
    ) -> SubTreeRecord:
        """Enroll a feature-authority sub-tree (convenience).

        feature_id should use the "feature:" prefix convention,
        e.g. "feature:age-over-18".
        """
        return self.enroll(feature_id, SubTreeKind.FEATURE, initial_sub_root, timestamp)

    # -- sub-root updates ----------------------------------------------------

    def update_sub_root(
        self, sub_tree_id: str, new_sub_root: int,
        timestamp: Optional[float] = None,
    ) -> int:
        """Update a sub-tree's root in the aggregator.

        Replaces the aggregator leaf at the sub-tree's slot; the aggregator
        root is recomputed.  Returns the new aggregator root.
        """
        import time
        rec = self._sub_trees.get(sub_tree_id)
        if rec is None:
            raise KeyError(f"unknown sub-tree: {sub_tree_id}")
        ts = timestamp if timestamp is not None else time.time()
        self._tree.leaves[rec.aggregator_leaf_index] = new_sub_root
        self._tree._root_dirty = True
        rec.sub_root = new_sub_root
        rec.updated_at = ts
        return self.identity_root

    # -- proofs --------------------------------------------------------------

    def aggregator_proof(self, sub_tree_id: str) -> AggregatorMembershipProof:
        """Proof that a sub-tree's current root is in the aggregator tree."""
        rec = self._sub_trees.get(sub_tree_id)
        if rec is None:
            raise KeyError(f"unknown sub-tree: {sub_tree_id}")
        mp = self._tree.path(rec.aggregator_leaf_index)
        return AggregatorMembershipProof(
            sub_root=rec.sub_root,
            siblings=list(mp.siblings),
            index_bits=list(mp.index_bits),
            aggregator_root=mp.root,
            sub_tree_id=sub_tree_id,
            aggregator_leaf_index=rec.aggregator_leaf_index,
        )

    def full_proof(
        self,
        sub_tree_id: str,
        sub_tree_proof: MembershipProof,
        M_x: int = 0,
        M_y: int = 0,
    ) -> FullMembershipProof:
        """Combine a sub-tree proof with the aggregator proof.

        Args:
            sub_tree_id: Which sub-tree the proof is in.
            sub_tree_proof: MembershipProof from the sub-tree.
            M_x: Identity point x coordinate (for the SNARK G1 tie).
            M_y: Identity point y coordinate.

        Returns:
            FullMembershipProof combining both paths.

        Raises:
            KeyError: If sub_tree_id is not enrolled.
            ValueError: If sub_tree_proof.root != the recorded sub_root.
        """
        rec = self._sub_trees.get(sub_tree_id)
        if rec is None:
            raise KeyError(f"unknown sub-tree: {sub_tree_id}")
        if sub_tree_proof.root != rec.sub_root:
            raise ValueError(
                f"sub_tree root ({sub_tree_proof.root:#x}) does not match "
                f"recorded sub_root ({rec.sub_root:#x})"
            )
        agg_proof = self.aggregator_proof(sub_tree_id)
        return FullMembershipProof(
            sub_tree_proof=sub_tree_proof,
            aggregator_proof=agg_proof,
            M_x=M_x, M_y=M_y,
        )

    def composed_proof(
        self,
        sub_tree_proofs: List[Tuple[str, MembershipProof]],
        M_x: int = 0,
        M_y: int = 0,
    ) -> ComposedMembershipProof:
        """Create an AND-composed proof across multiple sub-trees.

        Args:
            sub_tree_proofs: List of (sub_tree_id, MembershipProof) pairs.
            M_x: Identity point x coordinate.
            M_y: Identity point y coordinate.

        Returns:
            ComposedMembershipProof with one FullMembershipProof per sub-tree.
            All aggregator paths verify against the same identityRoot.
        """
        fulls = [
            self.full_proof(st_id, mp, M_x, M_y)
            for st_id, mp in sub_tree_proofs
        ]
        return ComposedMembershipProof(proofs=fulls)

    # -- batch operations ----------------------------------------------------

    def batch_update_and_root(
        self,
        updates: List[Tuple[str, int]],   # List[(sub_tree_id, new_sub_root)]
        timestamp: Optional[float] = None,
    ) -> int:
        """Apply multiple sub-root updates and return the new aggregator root.

        This is the transaction the central service posts on chain:
        IdentityRegistry.updateIdentityRoot(new_root, ...).
        """
        for sub_tree_id, new_sub_root in updates:
            self.update_sub_root(sub_tree_id, new_sub_root, timestamp)
        return self.identity_root

    # -- snapshot ------------------------------------------------------------

    def snapshot(self) -> dict:
        """Export the service state for persistence."""
        return {
            "depth": self._tree.depth,
            "leaves": self._tree.leaves[:],
            "sub_trees": {
                sid: {
                    "kind": rec.kind,
                    "sub_root": str(rec.sub_root),
                    "updated_at": rec.updated_at,
                    "aggregator_leaf_index": rec.aggregator_leaf_index,
                }
                for sid, rec in self._sub_trees.items()
            },
        }

    def __repr__(self) -> str:
        kyc = sum(1 for r in self._sub_trees.values() if r.is_kyc)
        feat = sum(1 for r in self._sub_trees.values() if r.is_feature)
        return (f"CentralMerkleService(kyc={kyc}, features={feat}, "
                f"identity_root={self.identity_root:#x})")


__all__ = [
    "CentralMerkleService",
    "SubTreeKind",
    "SubTreeRecord",
    "AggregatorMembershipProof",
    "FullMembershipProof",
    "ComposedMembershipProof",
]
