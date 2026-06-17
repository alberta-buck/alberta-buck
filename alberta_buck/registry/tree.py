"""Identity Merkle tree -- Poseidon accumulator of registered identity points.

Each leaf is identity_leaf(M) = Poseidon([M.x, M.y] % F_R), matching the
circuits/identity_membership.circom circuit byte-for-byte.  The tree is an
incremental Poseidon Merkle tree with configurable depth, supporting batch
insertion, membership proofs, and deterministic reconstruction from an event log.

Two uses:
1. Registry / Feature sub-tree -- each authority inserts identity points it has
   certified.  The sub-root goes to the CentralMerkleService.
2. Aggregator tree -- the CentralMerkleService inserts sub-roots from authorities;
   its root is the on-chain identityRoot.

A full membership proof chains the two: a path from the identity leaf to its
sub-root, plus a path from the sub-root to the aggregator root.

Reference:
- alberta_buck.wallet.unilateral_a2.IdentityTree (reference implementation)
- circuits/identity_membership.circom (the in-circuit verifier)
- alberta-buck-notes.org ("Mutual Decryptability", registry-Identity accumulator); see also alberta-buck-notes-flow.org "commit-before-use". Section "The Registry-Identity Accumulator" concept from the identity-axis design.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from alberta_buck.wallet.bn254 import ORDER, point_to_words
from alberta_buck.wallet.poseidon import F_R, poseidon


def identity_leaf(M) -> int:
    """Compute the Poseidon leaf hash for an identity point M.

    Matches the IdentityMembership circom circuit's leafH = Poseidon(2) with
    inputs Mx, My (auto-reduced mod F_R in circom).  The wallet's identity_leaf
    in alberta_buck.wallet.unilateral_a2 uses the identical formula.

    Args:
        M: A BN254 G1 point.

    Returns:
        Poseidon hash as a field element in [0, F_R).
    """
    x, y = point_to_words(M)
    return poseidon([x % F_R, y % F_R])


# Sentinel for an empty leaf (depth-0 zero).
EMPTY_LEAF = 0


@dataclass(frozen=True)
class MembershipProof:
    """A Merkle proof that a specific leaf is in the tree under a given root.

    Mirrors the MerkleProof template in circuits/identity_membership.circom.
    siblings[d] is the sibling hash at depth d; index_bits[d] is 0 if the leaf
    was on the left at that level, 1 if on the right.

    Attributes:
        leaf: The identity_leaf value being proven.
        siblings: Sibling hashes at each depth (length = tree depth).
        index_bits: Direction bits at each depth (0=left, 1=right).
        root: The tree root this proof is valid against.
        leaf_index: Absolute position of this leaf in the tree.
    """
    leaf: int
    siblings: List[int]
    index_bits: List[int]
    root: int
    leaf_index: int

    def verify(self) -> bool:
        """Recompute the path and check against root.

        Returns:
            True iff folding leaf up the path yields root.
        """
        cur = self.leaf
        for sib, bit in zip(self.siblings, self.index_bits):
            cur = poseidon([cur, sib]) if bit == 0 else poseidon([sib, cur])
        return cur == self.root

    def __repr__(self) -> str:
        return (f"MembershipProof(leaf={self.leaf:#x}, root={self.root:#x}, "
                f"index={self.leaf_index}, depth={len(self.siblings)})")


class IdentityMerkleTree:
    """Incremental Poseidon Merkle tree of identity leaves.

    Depth d supports up to 2^d leaves.  Insertion is incremental: leaves land
    at the next sequential index.  Uses Tornado-style filled_subtrees for O(d)
    insertion and O(d) path generation without storing internal nodes.

    Args:
        depth: Tree depth (1..32).  Depth 12 = ~4K identities per sub-tree.
    """

    def __init__(self, depth: int = 12) -> None:
        if depth < 1 or depth > 32:
            raise ValueError(f"depth must be in [1, 32], got {depth}")
        self.depth = depth
        self.leaves: List[int] = []

        # zeros[d] = root of an all-empty subtree of height d
        self._zeros: List[int] = [EMPTY_LEAF] * (depth + 1)
        for d in range(1, depth + 1):
            self._zeros[d] = poseidon([self._zeros[d - 1], self._zeros[d - 1]])

        # filled_subtrees[d] = rightmost known node at depth d
        self._filled: List[int] = [self._zeros[d] for d in range(depth)]

        self._root: Optional[int] = None
        self._root_dirty: bool = True

    # -- properties ----------------------------------------------------------

    @property
    def count(self) -> int:
        """Number of leaves inserted."""
        return len(self.leaves)

    @property
    def next_index(self) -> int:
        """Index the next insertion will occupy."""
        return len(self.leaves)

    def root(self) -> int:
        """Current Merkle root of the tree."""
        if self._root_dirty:
            self._root = self._compute_root()
            self._root_dirty = False
        return self._root

    # -- insertion -----------------------------------------------------------

    def insert_leaf(self, leaf: int) -> int:
        """Insert a pre-computed identity leaf.

        Args:
            leaf: The Poseidon identity_leaf value to insert.

        Returns:
            The leaf's index in the tree.
        """
        idx = len(self.leaves)
        self.leaves.append(leaf)
        self._insert_leaf_inner(leaf)
        self._root_dirty = True
        return idx

    def insert_identity(self, M) -> int:
        """Insert an identity point.

        Args:
            M: A BN254 G1 point representing the identity.

        Returns:
            The leaf's index in the tree.
        """
        return self.insert_leaf(identity_leaf(M))

    def insert_batch(self, leaves: List[int]) -> int:
        """Insert multiple pre-computed leaves atomically.

        Args:
            leaves: List of identity_leaf values.

        Returns:
            Index of the first inserted leaf.
        """
        if not leaves:
            return self.next_index
        first = len(self.leaves)
        for leaf in leaves:
            self.leaves.append(leaf)
            self._insert_leaf_inner(leaf)
        self._root_dirty = True
        return first

    def _insert_leaf_inner(self, leaf: int) -> None:
        """Update filled_subtrees for one leaf (Tornado-style incremental insertion)."""
        idx = len(self.leaves) - 1
        cur = leaf
        for d in range(self.depth):
            if idx & 1 == 0:       # left child: fill this slot
                self._filled[d] = cur
                cur = poseidon([cur, self._zeros[d]])
            else:                   # right child: fold with filled sibling
                cur = poseidon([self._filled[d], cur])
            idx >>= 1

    # -- path generation -----------------------------------------------------

    def path(self, index: int) -> MembershipProof:
        """Generate the Merkle authentication path for leaf at 'index'.

        Args:
            index: Leaf position (0-based).

        Returns:
            MembershipProof with siblings and direction bits.

        Raises:
            IndexError: If index is out of range.
        """
        if index < 0 or index >= len(self.leaves):
            raise IndexError(f"leaf index {index} out of range [0, {len(self.leaves)})")
        leaf = self.leaves[index]
        root = self.root()
        siblings, bits = self._derive_path(index)
        return MembershipProof(
            leaf=leaf, siblings=siblings, index_bits=bits,
            root=root, leaf_index=index,
        )

    def _derive_path(self, index: int) -> Tuple[List[int], List[int]]:
        """Build the sibling path for leaf at 'index' against the current root.

        Builds the full tree layer-by-layer from all leaves (like _compute_root),
        then walks down the path, extracting the sibling at each level.  This
        correctly accounts for leaves inserted *after* 'index' that affect
        siblings at higher levels.
        """
        # Build the complete layer structure from all leaves.
        nodes = list(self.leaves)
        layers: List[List[int]] = [nodes]  # layer 0 = leaves
        for d in range(self.depth):
            nxt: List[int] = []
            for i in range(0, len(nodes), 2):
                left = nodes[i]
                right = nodes[i + 1] if i + 1 < len(nodes) else self._zeros[d]
                nxt.append(poseidon([left, right]))
            layers.append(nxt)
            nodes = nxt

        # Walk down from the top, extracting the sibling at each level.
        siblings: List[int] = []
        bits: List[int] = []
        pos = index
        for d in range(self.depth):
            bit = pos & 1
            bits.append(bit)
            # Sibling is the node at position pos ^ 1 in layer d.
            layer = layers[d]
            sib_pos = pos ^ 1
            if sib_pos < len(layer):
                siblings.append(layer[sib_pos])
            else:
                siblings.append(self._zeros[d])
            pos >>= 1

        return siblings, bits

    # -- lookup --------------------------------------------------------------

    def contains(self, leaf: int) -> bool:
        """True if 'leaf' has been inserted."""
        return leaf in self.leaves

    def index_of_leaf(self, leaf: int) -> int:
        """Position of 'leaf' in the tree.

        Raises:
            ValueError: If 'leaf' is not in the tree.
        """
        return self.leaves.index(leaf)

    def index_of_identity(self, M) -> int:
        """Position of an identity point's leaf in the tree."""
        return self.leaves.index(identity_leaf(M))

    # -- internal ------------------------------------------------------------

    def _compute_root(self) -> int:
        """Recompute the root from the leaves by explicit layer folding."""
        if not self.leaves:
            return self._zeros[self.depth]
        nodes = list(self.leaves)
        for d in range(self.depth):
            nxt: List[int] = []
            for i in range(0, len(nodes), 2):
                left = nodes[i]
                right = nodes[i + 1] if i + 1 < len(nodes) else self._zeros[d]
                nxt.append(poseidon([left, right]))
            nodes = nxt
        return nodes[0]

    # -- rebuild from event log ----------------------------------------------

    @classmethod
    def from_leaves(cls, leaves: List[int], depth: int = 12) -> 'IdentityMerkleTree':
        """Reconstruct the tree from a leaf list (e.g. event log replay).

        Args:
            leaves: List of identity_leaf values in insertion order.
            depth: Tree depth (must match the original tree).

        Returns:
            IdentityMerkleTree with the same state.
        """
        tree = cls(depth=depth)
        if leaves:
            tree.insert_batch(leaves)
        return tree

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict:
        """Export tree state as a plain dict."""
        return {
            "depth": self.depth,
            "leaves": [str(leaf) for leaf in self.leaves],
            "root": str(self.root()),
        }

    def __repr__(self) -> str:
        return (f"IdentityMerkleTree(depth={self.depth}, "
                f"count={len(self.leaves)}, root={self.root():#x})")


__all__ = [
    "IdentityMerkleTree",
    "MembershipProof",
    "identity_leaf",
    "EMPTY_LEAF",
]
