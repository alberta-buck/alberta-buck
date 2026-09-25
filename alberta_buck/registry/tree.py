"""Identity Merkle tree -- Poseidon accumulator of registered identity points.

Each leaf is one of four tagged Poseidon commitments -- identity_leaf(M) =
Poseidon(TAG, M.x, M.y), and its salted, receiving and mailbox siblings -- each
led by its own field-element tag (alberta_buck.wallet.domains, LEAF_*), so no
value is a leaf of two kinds.  The circuits hash the same tags.  The tree is an
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
from alberta_buck.wallet.domains import (
    LEAF_IDENTITY, LEAF_IDENTITY_SALTED, LEAF_MAILBOX, LEAF_RECEIVING, field_tag,
)
from alberta_buck.wallet.poseidon import F_R, poseidon

# The leaf kinds' leading Poseidon inputs.  Two of the untagged leaves were
# three-input Poseidons, so one value could be a salted identity leaf and a
# receiving leaf at once; a tag per kind makes each its own function.
TAG_IDENTITY                    = field_tag(LEAF_IDENTITY)
TAG_IDENTITY_SALTED             = field_tag(LEAF_IDENTITY_SALTED)
TAG_RECEIVING                   = field_tag(LEAF_RECEIVING)
TAG_MAILBOX                     = field_tag(LEAF_MAILBOX)


def identity_leaf(M) -> int:
    """Compute the leaf Poseidon(TAG_IDENTITY, M.x, M.y) for an identity point M.

    The coordinates are reduced mod F_R, as circom reduces its signals.  The
    wallet's identity_leaf in alberta_buck.wallet.unilateral_a2 is this function.

    Args:
        M: A BN254 G1 point.

    Returns:
        Poseidon hash as a field element in [0, F_R).
    """
    x, y = point_to_words(M)
    return poseidon([TAG_IDENTITY, x % F_R, y % F_R])


def identity_leaf_salted(M, salt: int) -> int:
    """Compute the hiding leaf commitment Poseidon(TAG_IDENTITY_SALTED, M.x, M.y, salt).

    The leaf of a PRIVATE subtree (accumulator specification, section 4):
    membership is a fact about a person who did not publish it, so the leaf
    must not be a deterministic function of the identity.  A party holding
    any set of identity scalars -- including a registry holding every scalar
    it ever certified -- cannot decide membership without the salt.

    Rationale for a second function rather than a changed one: the unsalted
    identity_leaf is retained unchanged for PUBLIC subtrees (a regulator's
    insurers, whose membership they advertise), so every committed vector
    that records it stays valid.

    Args:
        M: A BN254 G1 point.
        salt: A blinding value in [1, F_R).  Zero is refused: it would make
            the leaf deterministic in a tree declared private.

    Returns:
        Poseidon hash as a field element in [0, F_R).

    Raises:
        ValueError: if salt is outside [1, F_R).
    """
    if not isinstance(salt, int) or not (1 <= salt < F_R):
        raise ValueError("salt must be in [1, F_R); 0 makes the leaf deterministic")
    x, y = point_to_words(M)
    return poseidon([TAG_IDENTITY_SALTED, x % F_R, y % F_R, salt])


def receiving_leaf(m_rec: int, k_recv: int, salt: int) -> int:
    """Compute the hiding leaf commitment Poseidon(TAG_RECEIVING, m_rec, k_recv, salt).

    The leaf of a private IDENTITY-REGISTRY subtree, which must bind two
    things rather than one: the Identity a Note names, and the receiving key a
    Note is encrypted to.

    Why the pair belongs in one leaf.  Addressed Notes are keyed to
    ``pk_recv = k*G``, not to the identity point, because an identity scalar is
    a read capability the design discloses to every counterparty and so cannot
    also be a decryption key (alberta_buck.wallet.recvkey).  That separation
    buys the privacy and creates an obligation: a note addressed to a key of
    the payer's choosing would break mutual decryptability and the receipt, so
    the receiving key MUST be bound to the Identity -- and the spend gate must
    prove that binding rather than assume it.

    Why the SCALARS and not the points.  Its two siblings commit coordinates
    because the authorities that compute them hold identity POINTS and nothing
    else.  This leaf is different in kind: its whole purpose is to be proven in
    zero knowledge, and the prover holds the scalars.  Committing the points
    would force the circuit to re-derive them, at 471,896 constraints per
    fixed-base multiplication -- 943,792 to hash a commitment whose preimages
    the prover already has.  Committing the scalars costs one Poseidon.

    Three further things follow, and each is an improvement rather than a
    trade:

      * BN254's G1 group order equals the Poseidon field, so a scalar IS a
        native field element.  No reduction, no limbs, and no aliasing
        question about a limb decomposition.
      * The circuit hashes the very same private signals its other relations
        use, so the tie between "the Identity in the credential" and "the
        Identity in the leaf" is direct rather than mediated by a point
        derivation whose output limbs the gadget does not range-check.
      * It is strictly harder to scan.  A payer is GIVEN both ``M_rec`` and
        ``pk_recv``, so under a coordinate-committing leaf only the salt stood
        between it and a membership test.  Here it would need ``k`` as well,
        and ``k`` is disclosed to nobody.

    Why it is committed rather than published.  A spend proving against a
    PUBLIC binding would reveal the recipient's registered receiving key and
    deanonymise them to everyone, which is worse than the problem being
    solved.  Under the holder's own salt the leaf is provable in zero
    knowledge and unscannable to a party holding every certified identity and
    the whole published subtree.

    What it prevents.  A gate that proved "I can read this note" and "I am
    this registered Identity" side by side would state nothing about their
    owner: a thief holding a stolen payload supplies the reading half with the
    stolen key and the Identity half with its OWN registered Identity, both
    true, neither joining them.  This leaf is the relation that joins them,
    and it is finding 5's lesson in a second place -- never infer equality
    from two proofs that merely share a public point.

    Args:
        m_rec: The holder's identity scalar, in [1, F_R).
        k_recv: The holder's receiving secret, in [1, F_R).
        salt: The holder's blinding value for THIS subtree, in [1, F_R).

    Returns:
        Poseidon hash as a field element in [0, F_R).

    Raises:
        ValueError: if any argument is outside [1, F_R).
    """
    for name, val in (("m_rec", m_rec), ("k_recv", k_recv), ("salt", salt)):
        if not isinstance(val, int) or not (1 <= val < F_R):
            raise ValueError(f"{name} must be in [1, F_R)")
    return poseidon([TAG_RECEIVING, m_rec, k_recv, salt])


def mailbox_leaf(M, pk_recv, salt: int) -> int:
    """Compute Poseidon(TAG_MAILBOX, M.x, M.y, pk_recv.x, pk_recv.y, salt) -- the PAYER's
    view of the same association :func:`receiving_leaf` commits.

    Two leaves for one fact, because it has two consumers that hold different
    things, and neither leaf serves the other's consumer:

      * The SPEND proves the association in zero knowledge, and the prover
        holds the scalars, so :func:`receiving_leaf` commits ``(m_rec, k)`` and
        costs one Poseidon.  Committing the points there would force the
        circuit to re-derive them at 471,896 constraints apiece.
      * A PAYER must check the association BEFORE paying, and holds no secret
        at all -- only the two points, which it needs anyway: ``M_rec`` to name
        the recipient in a receipt and ``pk_recv`` to address the note.  It
        cannot open a scalar leaf without ``k``, and handing over ``k`` hands
        over the mailbox, in both directions in time.  So the payer's leaf
        commits the POINTS, and checking it is a hash and a path.

    That is why this is a leaf and not a proof.  The alternative -- a circuit
    proving the scalar leaf's preimage in zero knowledge -- costs a
    fixed-base multiplication, a trusted setup and a Groth16 verifier inside
    every receipt checker, to establish a fact that one more Poseidon
    establishes for free.  The scalar leaf stays exactly as it is, because the
    gate's constraint budget is what forced it and this changes nothing there.

    Distinct associations carry distinct salts (accumulator specification
    section 8.3), so the salt a holder discloses to a payer here says nothing
    about the salt its spend proves under.  A payer given this salt can locate
    THIS leaf in the published subtree and nothing else: it learns that an
    Identity it already knows has a mailbox key it was already given.

    Args:
        M: The Identity point.
        pk_recv: The receiving key ``k*G``.
        salt: The holder's blinding value for this association, in [1, F_R).

    Returns:
        Poseidon hash as a field element in [0, F_R).

    Raises:
        ValueError: if salt is outside [1, F_R).
    """
    if not isinstance(salt, int) or not (1 <= salt < F_R):
        raise ValueError("salt must be in [1, F_R); 0 makes the leaf deterministic")
    mx, my = point_to_words(M)
    px, py = point_to_words(pk_recv)
    return poseidon([TAG_MAILBOX, mx % F_R, my % F_R, px % F_R, py % F_R, salt])


# --- Tree depths ---------------------------------------------------------- #
#
# THREE depths, and they are meant to differ.  This has been mistaken for an
# inconsistency more than once, so the numbers live here as names rather than
# as literals at each call site.
#
#   AGGREGATOR_DEPTH      the CentralMerkleService tree whose root IS the
#                         on-chain `identityRoot`.  Must equal Solidity
#                         `IdentityRegistry.IDENTITY_TREE_DEPTH` and the
#                         `component main` depth of both membership circuits,
#                         because those prove a path to that exact root.
#
#   KYC_SUBTREE_DEPTH     one registry organization's own identity sub-tree.
#                         Deliberately deeper: it sizes how many identities a
#                         single organization can hold (2**12 ~ 4K), which is
#                         a capacity question, not a protocol one.  Changing
#                         it changes sub-roots and therefore the committed
#                         cross-language kernel vectors.
#
#   FEATURE_SUBTREE_DEPTH a feature authority's sub-tree (attributes such as
#                         licences).  Equal to AGGREGATOR_DEPTH today by
#                         coincidence of capacity, not by requirement -- kept
#                         separate so raising one does not silently raise the
#                         other.
#
# Only AGGREGATOR_DEPTH is protocol-critical.  The two sub-tree depths are
# capacity knobs and may be raised independently, at the cost of regenerating
# the vectors that pin them (core/vectors/registry-kernel-vectors.json records
# aggregator.depth beside reg_a.depth / reg_b.depth).
AGGREGATOR_DEPTH: int = 20
KYC_SUBTREE_DEPTH: int = 12
FEATURE_SUBTREE_DEPTH: int = 10

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
        depth: Tree depth (1..32).  Depth 12 = ~4K identities per SUB-TREE.

            12 is deliberately NOT the chain's IDENTITY_TREE_DEPTH (10), and
            they are not supposed to agree.  An identity organization keeps
            its own sub-tree at this depth; the depth-10 AGGREGATOR composes
            those sub-roots into the single on-chain `identityRoot` (see
            merkle_service.AggregatorMembershipProof, whose `aggregator_root`
            IS that on-chain value).  The committed cross-language vectors
            pin the split: core/vectors/registry-kernel-vectors.json carries
            aggregator.depth = 10 beside reg_a.depth = reg_b.depth = 12.

            So do not "reconcile" this default with the contract.  Changing
            it silently changes every sub-tree root and invalidates the
            kernel vectors that Rust, Python and JS all replay.
    """

    def __init__(self, depth: int = KYC_SUBTREE_DEPTH, private: bool = False) -> None:
        if depth < 1 or depth > 32:
            raise ValueError(f"depth must be in [1, 32], got {depth}")
        self.depth = depth
        self.private = private
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
        """Insert an identity point under the UNSALTED leaf.

        Only valid on a public subtree.  On a private one the leaf would be
        a deterministic function of the identity, which is the scan the
        accumulator exists to prevent, so this raises.

        Args:
            M: A BN254 G1 point representing the identity.

        Returns:
            The leaf's index in the tree.

        Raises:
            ValueError: if the tree was declared private.
        """
        if self.private:
            raise ValueError(
                "private subtree: use insert_identity_salted; an unsalted leaf "
                "is a deterministic function of the identity")
        return self.insert_leaf(identity_leaf(M))

    def insert_identity_salted(self, M, salt: int) -> int:
        """Insert an identity point under the hiding leaf commitment.

        The insertion an authority performs for a private subtree: the holder
        derives the salt (alberta_buck.wallet.salt) and sends (M, salt); the
        authority, which knows M already, computes the leaf and inserts it.
        No proof is required or useful at admission.

        Args:
            M: A BN254 G1 point representing the identity.
            salt: The holder's blinding value for THIS subtree.

        Returns:
            The leaf's index in the tree.
        """
        return self.insert_leaf(identity_leaf_salted(M, salt))

    def insert_receiving(self, m_rec: int, k_recv: int, salt: int) -> int:
        """Insert the (Identity, receiving key) pair under the hiding leaf.

        The admission an identity registry performs: the holder derives the
        salt and the receiving key from its own seed material
        (alberta_buck.wallet.salt, alberta_buck.wallet.recvkey) and sends
        ``(M, pk_recv, salt)``; the authority, which knows ``M`` already,
        computes the leaf and inserts it.  The identity registry holds the
        identity SCALAR it certified, so it can still check the leaf it
        inserts.  No proof is required or useful at admission: a holder who
        lies about its own receiving key, or about its own identity, only
        makes its own notes unspendable -- the spend gate's credential
        relation holds the true identity.

        Rotation is a re-association: insert at an incremented counter and
        clear the old leaf (accumulator specification, section 8.3).  The two
        leaves share no salt, so they do not link.

        Args:
            m_rec: The holder's identity scalar.
            k_recv: The holder's receiving secret for addressed Notes.
            salt: The holder's blinding value for THIS subtree.

        Returns:
            The leaf's index in the tree.
        """
        return self.insert_leaf(receiving_leaf(m_rec, k_recv, salt))

    def insert_mailbox(self, M, pk_recv, salt: int) -> int:
        """Admit the PAYER's view of a receiving-key association.

        The sibling of :meth:`insert_receiving`: the same association, committed
        over the points so a payer with no secret can check it before paying
        (:func:`mailbox_leaf`).  A holder that wants both consumers served
        admits both leaves, under DIFFERENT salts.

        Args:
            M: The Identity point.
            pk_recv: The receiving key.
            salt: The holder's blinding value for this association -- NOT the
                salt of the leaf its spend proves under.

        Returns:
            The leaf's index in the tree.
        """
        return self.insert_leaf(mailbox_leaf(M, pk_recv, salt))

    def clear_leaf(self, index: int) -> int:
        """Clear a leaf to EMPTY_LEAF: the revocation primitive.

        The incremental tree supports no removal, so a revoked member's leaf
        is set to the empty sentinel and the root recomputed.  The authority
        must then push the fresh sub-root; until it does, the member can
        still prove membership against the previously posted root, which is
        what a consumer's maximum root age bounds.

        Clearing is visible in a published subtree.  It reveals that a
        member was removed, not which, because the leaf it replaced was a
        hiding commitment.

        Args:
            index: Position of the leaf to clear.

        Returns:
            The leaf value that was cleared.

        Raises:
            IndexError: if there is no leaf at `index`.
        """
        if not 0 <= index < len(self.leaves):
            raise IndexError(f"no leaf at index {index}")
        old = self.leaves[index]
        self.leaves[index] = EMPTY_LEAF
        self._root_dirty = True
        return old

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
    def from_leaves(cls, leaves: List[int],
                    depth: int = KYC_SUBTREE_DEPTH) -> 'IdentityMerkleTree':
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
    "identity_leaf_salted",
    "receiving_leaf",
    "mailbox_leaf",
    "EMPTY_LEAF",
]
