"""Feature Authority -- certifies that identity points possess specific attributes.

A FeatureAuthority represents an attribute certifier (e.g. "age verification
authority", "driver's license bureau", "regional residency office").  It maintains
a Poseidon Merkle tree of identity points that possess the feature.  Unlike a KYC
registry, a feature authority does NOT issue certificates -- it simply inserts
existing identity points M (already certified by a KYC registry) into its tree
when the identity holder proves they possess the attribute.

The feature authority pushes its sub-root to the CentralMerkleService, which
aggregates it with KYC registry sub-roots into the single on-chain identityRoot.
An identity holder can then produce an AND-composed proof: "M is in the KYC
registry's tree AND M is in the age>18 feature tree", both verifying against the
same identityRoot via separate aggregator paths.

An identity M appears in as many feature trees as it possesses attributes.
At spend time, the prover selects which features to prove; the SNARK hides
which feature trees were used (all aggregator paths lead to the same public
identityRoot).

Reference: alberta-buck-notes-identity-axis.org.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

from alberta_buck.registry.tree import IdentityMerkleTree, MembershipProof, identity_leaf
from alberta_buck.wallet.bn254 import G1, mul, eq


# ---------------------------------------------------------------------------
# Feature record
# ---------------------------------------------------------------------------

@dataclass
class FeatureRecord:
    """A single identity-feature attestation.

    Attributes:
        M: The identity point possessing this feature.
        leaf: identity_leaf(M).
        leaf_index: Position in the feature authority's tree.
        attested_at: POSIX timestamp of attestation.
        evidence_hash: Optional hash of the evidence (e.g. keccak256 of
            a driver's license scan).  Stored for audit; never revealed on chain.
    """
    M: Tuple
    leaf: int
    leaf_index: int
    attested_at: float
    evidence_hash: Optional[bytes]


# ---------------------------------------------------------------------------
# Feature Authority
# ---------------------------------------------------------------------------

class FeatureAuthority:
    """An attribute certifier that maintains a Merkle tree of qualified identities.

    Feature authorities do not issue certificates; they attest that an existing
    KYC-certified identity M possesses a specific attribute.  The authority
    verifies evidence off-chain (e.g. checking a driver's license database) and
    inserts M into its tree.

    Args:
        feature_id: Stable identifier (e.g. "feature:age-over-18").
        tree_depth: Depth of the feature tree (default 10, ~1K identities).
    """

    def __init__(self, feature_id: str, tree_depth: int = 10) -> None:
        if not feature_id.startswith("feature:"):
            raise ValueError("feature_id must use the 'feature:' prefix convention")
        self.feature_id = feature_id
        self._tree = IdentityMerkleTree(depth=tree_depth)
        self._records: Dict[int, FeatureRecord] = {}  # leaf_index -> record

    # -- properties ----------------------------------------------------------

    @property
    def sub_root(self) -> int:
        """Current root of the feature tree."""
        return self._tree.root()

    @property
    def identity_count(self) -> int:
        """Number of identities attested for this feature."""
        return self._tree.count

    # -- attestation ---------------------------------------------------------

    def attest(
        self,
        M,                              # BN254 G1 point
        evidence_hash: Optional[bytes] = None,
    ) -> FeatureRecord:
        """Attest that identity point M possesses this feature.

        The caller must have proven possession of the attribute to the authority
        off-chain.  M is inserted into the feature tree; the new sub_root must
        be pushed to the CentralMerkleService before the attestation is usable
        on chain (commit-before-use).

        Args:
            M: The identity point (must already be KYC-certified by a registry).
            evidence_hash: Optional hash of the evidence for audit trails.

        Returns:
            FeatureRecord with the tree position.

        Raises:
            ValueError: If M is already in the tree (duplicate attestation).
        """
        leaf = identity_leaf(M)
        if self._tree.contains(leaf):
            raise ValueError(f"identity already attested for feature {self.feature_id}")
        leaf_index = self._tree.insert_leaf(leaf)
        rec = FeatureRecord(
            M=M, leaf=leaf, leaf_index=leaf_index,
            attested_at=time.time(), evidence_hash=evidence_hash,
        )
        self._records[leaf_index] = rec
        return rec

    def attest_batch(
        self,
        identities: List['Tuple'],  # List[M] — identity points
        evidence_hashes: Optional[List[Optional[bytes]]] = None,
    ) -> List[FeatureRecord]:
        """Attest multiple identities in one batch.

        Args:
            identities: List of BN254 G1 identity points to attest.
            evidence_hashes: Optional parallel list of evidence hashes
                (one per identity, or None for no evidence).  Must be the
                same length as identities if provided.

        Returns:
            List of FeatureRecord, one per attested identity.
        """
        records = []
        evs = evidence_hashes if evidence_hashes is not None else [None] * len(identities)
        if len(evs) != len(identities):
            raise ValueError("evidence_hashes length must match identities length")
        for M, ev in zip(identities, evs):
            rec = self.attest(M, ev)
            records.append(rec)
        return records

    def revoke(self, M) -> Optional[int]:
        """Revoke an attestation by removing M from the tree.

        In the current incremental Merkle tree, removal is not directly
        supported -- this sets the leaf to the empty-leaf sentinel and
        marks the root dirty.  A new sub_root must be pushed to the
        aggregator to complete the revocation.

        Args:
            M: The identity point to revoke.

        Returns:
            The leaf index that was cleared, or None if not found.
        """
        leaf = identity_leaf(M)
        if not self._tree.contains(leaf):
            return None
        idx = self._tree.index_of_leaf(leaf)
        self._tree.leaves[idx] = 0  # EMPTY_LEAF
        self._tree._root_dirty = True
        return idx

    # -- membership proofs ---------------------------------------------------

    def membership_proof(self, leaf_index: int) -> MembershipProof:
        """Generate a Merkle proof for the identity at leaf_index.

        Args:
            leaf_index: Position in the feature tree.

        Returns:
            MembershipProof valid against the current sub_root.
        """
        return self._tree.path(leaf_index)

    def membership_proof_for_identity(self, M) -> Optional[MembershipProof]:
        """Generate a Merkle proof for identity point M.

        Args:
            M: The BN254 G1 identity point.

        Returns:
            MembershipProof or None if M is not in the tree.
        """
        leaf = identity_leaf(M)
        if not self._tree.contains(leaf):
            return None
        idx = self._tree.index_of_leaf(leaf)
        return self._tree.path(idx)

    # -- lookup --------------------------------------------------------------

    def has_identity(self, M) -> bool:
        """True if M is attested for this feature."""
        return self._tree.contains(identity_leaf(M))

    def get_record(self, leaf_index: int) -> Optional[FeatureRecord]:
        """Look up an attestation record by leaf index."""
        return self._records.get(leaf_index)

    def __repr__(self) -> str:
        return (f"FeatureAuthority({self.feature_id!r}, "
                f"identities={self.identity_count}, "
                f"sub_root={self.sub_root:#x})")


__all__ = [
    "FeatureAuthority",
    "FeatureRecord",
]
