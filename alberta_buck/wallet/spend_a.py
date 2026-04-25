"""A-flavor spend (Phase 8) witness emitter and Python constraint oracle.

The on-chain ``spend_a.circom`` is not yet shipped; this module provides:

* The witness/public-input bundle a wallet would feed to ``snarkjs`` once
  the circuit lands (:class:`SpendAWitness`).
* A pure-Python constraint oracle (:func:`spend_a_satisfied`) that mirrors
  the planned circuit's constraint groups, so we can write rejection tests
  for the Phase 8 stop conditions before any circom code exists.
* A Tornado-style append-only Poseidon-2 Merkle tree (:class:`MerkleTree`)
  matching :file:`scripts/snark/prove_spend.js`'s off-chain reconstruction
  -- so the resulting ``noteRoot`` agrees with the on-chain
  filled-subtrees insertion in :file:`src/Notes.sol`.

Constraint groups (Phase 8 of :file:`alberta-buck-ethereum.org`):

* **(R)** range bounds: ``face`` and ``v`` in ``[0, 2^128)``; ``face == v``.
* **(C)** commitment opening: ``cm == Poseidon([flavor, v, rho, id_hash, predicate])``.
* **(M)** Merkle inclusion: walking ``(cm, sibs, bits)`` up ``TREE_DEPTH``
  Poseidon-2 hashes yields ``noteRoot``.
* **(N)** A-tag nullifier: ``nullifier == Poseidon([rho, id_hash, 4243])``
  (4243 domain-separates from the B-flavor's 4242).
* **(CP)** the Phase 8 cryptographic substance -- *single* ``sk_dep`` witness
  shared by the note-side and registry-side ElGamal openings::

      C_n - sk_dep * R_n  ===  C_reg - sk_dep * R_reg

  This forces ``pk_rec_mint === pk_dep_current`` at constraint time -- if the
  recipient lost their private key and re-registered under a new ElGamal key,
  no single ``sk_dep`` can satisfy the equality.  Per the 2026-04 Deepseek R4
  review, this is by design: A-note loss-recovery via identity re-issuance is
  impossible; ``sk_rec`` must be backed up like a hardware-wallet seed, loss
  is terminal.
* **(B)** ghost-binds for ``recipient`` and ``chainId`` so they participate in
  the Groth16 IC commitment (no oracle check needed -- circom-only concern).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, eq, mul, neg,
)
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.notes import (
    FLAVOR_A1, FLAVOR_A2, NULLIFIER_TAG_A,
    NoteOpening, note_commitment, nullifier_a,
)
from alberta_buck.wallet.poseidon import F_R, poseidon
from alberta_buck.wallet.transcript import keccak_raw

# Notes.sol pins TREE_DEPTH = 20.  Changing this re-templates every spend
# circuit; do not divorce from the on-chain constant.
TREE_DEPTH = 20

# Mirror Notes.sol L67: ZERO_VALUE = keccak256("AlbertaBuck:Notes:zero") % F_R.
# The constant below is the literal in Notes.sol -- pinned here so a Python
# Merkle build cannot drift from the on-chain seed.
ZERO_VALUE = int.from_bytes(keccak_raw(b"AlbertaBuck:Notes:zero"), "big") % F_R
assert ZERO_VALUE == 12478158023141672556814566805819277863195393802640872128727997243357085450959


def _build_zeros(depth: int) -> List[int]:
    """zeros[i] = empty-subtree root at level i (i in [0, depth))."""
    z = [ZERO_VALUE]
    for i in range(1, depth):
        z.append(poseidon([z[i - 1], z[i - 1]]))
    return z


_ZEROS: List[int] = _build_zeros(TREE_DEPTH)


# Empty-tree root: 20 levels of self-paired ZERO_VALUE.  Matches
# Notes.sol::EMPTY_ROOT (validated below at import time -- if this drifts
# the on-chain constructor seed and the Python tree disagree on the
# initial root and every spend witness against an empty tree breaks).
def _empty_root() -> int:
    r = ZERO_VALUE
    for _ in range(TREE_DEPTH):
        r = poseidon([r, r])
    return r


EMPTY_ROOT = _empty_root()
assert EMPTY_ROOT == 6959478139657271248173638342125700921600510448444968095526832403890386862787


@dataclass
class MerkleTree:
    """Tornado-style append-only Poseidon-2 Merkle tree at fixed depth.

    Layers are materialized against the *final* leaf set (insert all, then
    walk up).  Padding for missing right siblings uses ``_ZEROS[level]``.
    The resulting root agrees with :file:`src/Notes.sol`'s filled-subtrees
    insertion despite that contract capturing siblings at insertion time --
    both schemes compose the same Poseidon-2 over the same node payloads.
    """
    leaves: List[int] = field(default_factory=list)

    def append(self, leaf: int) -> int:
        if not (0 <= leaf < F_R):
            raise ValueError("leaf must lie in [0, F_R)")
        if len(self.leaves) >= (1 << TREE_DEPTH):
            raise ValueError("tree full")
        self.leaves.append(leaf)
        return len(self.leaves) - 1

    def _layers(self) -> List[List[int]]:
        layers: List[List[int]] = [list(self.leaves)]
        for level in range(TREE_DEPTH):
            cur = layers[level]
            nxt: List[int] = []
            for i in range(0, len(cur), 2):
                left = cur[i]
                right = cur[i + 1] if i + 1 < len(cur) else _ZEROS[level]
                nxt.append(poseidon([left, right]))
            layers.append(nxt)
        return layers

    def root(self) -> int:
        if not self.leaves:
            return EMPTY_ROOT
        top = self._layers()[TREE_DEPTH]
        return top[0] if top else EMPTY_ROOT

    def proof(self, idx: int) -> Tuple[List[int], List[int]]:
        """Return ``(siblings, bits)`` for the leaf at ``idx``.

        ``bits[level]`` is the LSB of ``idx >> level``: 0 means the current
        node is the left child at that level (so the sibling is on the right);
        1 means the current node is the right child.  Layout matches
        :file:`circuits/spend.circom`'s ``MerkleProof`` template.
        """
        if not (0 <= idx < len(self.leaves)):
            raise ValueError(f"idx out of range: {idx}")
        layers = self._layers()
        sibs: List[int] = []
        bits: List[int] = []
        for level in range(TREE_DEPTH):
            sib_idx = idx ^ 1
            sib = layers[level][sib_idx] if sib_idx < len(layers[level]) else _ZEROS[level]
            sibs.append(sib)
            bits.append(idx & 1)
            idx >>= 1
        return sibs, bits


def merkle_walk(leaf: int, siblings: Sequence[int], bits: Sequence[int]) -> int:
    """Walk the Merkle path bottom-up; return the resulting root.

    Mirrors the in-circuit walk: at each level, ``bits[l] == 0`` keeps the
    current node on the left, ``== 1`` swaps it to the right (per
    :file:`circuits/spend.circom`'s ``Switcher``).
    """
    if len(siblings) != TREE_DEPTH or len(bits) != TREE_DEPTH:
        raise ValueError("path length must be TREE_DEPTH")
    cur = leaf
    for sib, b in zip(siblings, bits):
        if b not in (0, 1):
            raise ValueError("bit must be 0 or 1")
        if b == 0:
            cur = poseidon([cur, sib])
        else:
            cur = poseidon([sib, cur])
    return cur


# --- A-spend witness/proof bundle ----------------------------------------

@dataclass(frozen=True)
class SpendAWitness:
    """Full witness an A-spend wallet would feed into ``spend_a.circom``.

    Public inputs (the on-chain ``Notes.spendA`` call sees these):

    * ``note_root``   -- Merkle root the proof binds to (must be in the
      contract's recent-roots window).
    * ``nullifier``   -- ``Poseidon([rho, id_hash, 4243])``; contract enforces
      single-use via the ``nullifiers`` mapping.
    * ``face``        -- released BUCK amount; equals ``v``.
    * ``recipient``   -- payout address (``msg.sender`` or wallet-chosen).
    * ``chain_id``    -- ``block.chainid``; binds the proof to a specific chain.

    Private witness:

    * ``flavor``      -- A1 or A2.
    * ``v``           -- face value (uint128).
    * ``rho``         -- per-note randomness; sources nullifier and binds cm.
    * ``id_hash``     -- collapsed identity payload from
      :func:`alberta_buck.wallet.notes.id_hash_a1` / ``id_hash_a2``.
    * ``predicate``   -- spend predicate hash (zero == "no predicate").
    * ``sibs``/``bits`` -- Merkle path from the leaf cm up to ``note_root``.
    * ``sk_dep``      -- spender's deposit secret key.  The **single** witness
      shared between the note-side and registry-side ElGamal openings; the
      key-pair-binding invariant is that one ``sk_dep`` decrypts both ``E_note``
      and ``E_reg`` iff ``pk_rec_mint == pk_dep_current``.
    * ``e_note``      -- ElGamal ciphertext from the note opening.
    * ``e_reg``       -- ElGamal ciphertext registered to the spender on-chain.
    """
    note_root:  int
    nullifier:  int
    face:       int
    recipient:  int
    chain_id:   int

    flavor:     int
    v:          int
    rho:        int
    id_hash:    int
    predicate:  int
    sibs:       Tuple[int, ...]
    bits:       Tuple[int, ...]
    sk_dep:     int
    e_note:     ElGamalCiphertext
    e_reg:      ElGamalCiphertext


def make_spend_a_witness(
    opening:    NoteOpening,
    tree:       MerkleTree,
    leaf_index: int,
    sk_dep:     int,
    e_note:     ElGamalCiphertext,
    e_reg:      ElGamalCiphertext,
    recipient:  int,
    chain_id:   int,
) -> SpendAWitness:
    """Assemble a :class:`SpendAWitness` from the wallet's local state.

    Asserts the leaf at ``leaf_index`` is the commitment for ``opening`` and
    that the opening carries an A-flavor; raises :class:`ValueError` on a
    mismatch so callers learn early about witness/leaf-state desyncs.
    Caller is responsible for supplying the correct ``e_note`` (carried in
    the note opening) and ``e_reg`` (looked up against the on-chain
    ``IdentityRegistry`` for the spender).
    """
    if opening.flavor not in (FLAVOR_A1, FLAVOR_A2):
        raise ValueError("spend_a: opening must carry an A-flavor (A1 or A2)")
    if not (0 < sk_dep < ORDER):
        raise ValueError("sk_dep must be a non-zero scalar mod ORDER")
    cm = note_commitment(opening)
    if not (0 <= leaf_index < len(tree.leaves)):
        raise ValueError("leaf_index out of range")
    if tree.leaves[leaf_index] != cm:
        raise ValueError("leaf at leaf_index does not match opening's cm")
    sibs, bits = tree.proof(leaf_index)
    return SpendAWitness(
        note_root=tree.root(),
        nullifier=nullifier_a(opening.rho, opening.id_hash),
        face=opening.v,
        recipient=recipient,
        chain_id=chain_id,
        flavor=opening.flavor,
        v=opening.v,
        rho=opening.rho,
        id_hash=opening.id_hash,
        predicate=opening.predicate,
        sibs=tuple(sibs),
        bits=tuple(bits),
        sk_dep=sk_dep,
        e_note=e_note,
        e_reg=e_reg,
    )


# --- Constraint oracle (mirrors planned spend_a.circom) ------------------

def spend_a_satisfied(w: SpendAWitness) -> bool:
    """Return True iff every spend_a constraint group accepts ``w``.

    This is the Python oracle for the planned ``circuits/spend_a.circom``
    -- letting us write Phase 8 rejection tests *before* the circuit is
    written.  Constraint groups match the spec; see module docstring.
    """
    # (R) face/v range and equality.
    if not (0 <= w.face < (1 << 128)):
        return False
    if not (0 <= w.v < (1 << 128)):
        return False
    if w.face != w.v:
        return False

    # (C) commitment opening.
    cm = poseidon([w.flavor, w.v, w.rho, w.id_hash, w.predicate])

    # (M) Merkle inclusion against note_root.
    if merkle_walk(cm, w.sibs, w.bits) != w.note_root:
        return False

    # (N) A-tag nullifier.
    if w.nullifier != poseidon([w.rho, w.id_hash, NULLIFIER_TAG_A]):
        return False

    # (CP) single-sk_dep ElGamal equality.  Both sides should equal M_rec
    # (= m_rec * G_1, the recipient's identity point), but we only check
    # equality of the two derivations -- that is exactly what forces a
    # single sk_dep to decrypt both ciphertexts and so makes
    # pk_rec_mint == pk_dep_current the only way to satisfy the constraint.
    sk = w.sk_dep % ORDER
    M_from_note = add(w.e_note.C, neg(mul(w.e_note.R, sk)))
    M_from_reg  = add(w.e_reg.C,  neg(mul(w.e_reg.R,  sk)))
    if not eq(M_from_note, M_from_reg):
        return False

    # (B) ghost-bind is a circom-only concern (no oracle check needed).
    return True


__all__ = [
    "TREE_DEPTH", "ZERO_VALUE", "EMPTY_ROOT",
    "MerkleTree", "merkle_walk",
    "SpendAWitness", "make_spend_a_witness", "spend_a_satisfied",
]
