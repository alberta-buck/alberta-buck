"""BUCK Notes commitment / nullifier construction (Phase 7+ corrected design).

Hash family: circomlib Poseidon over BN254 (matches the shipped
=spend.circom=).  The Python implementation
in :mod:`alberta_buck.wallet.poseidon` agrees with circomlibjs's unoptimized
variant, which is the same hash the optimized circuit Poseidon computes (just
via different but equivalent constants).  See :file:`scripts/snark/poseidon_t3_code.js`
for the on-chain bytecode story.

Wire formats::

    cm    = Poseidon([flavor, v, rho, id_hash, predicate])    # spend.circom (C)
    nf_b  = Poseidon([rho, id_hash, 4242])                    # spend.circom (N)
    nf_a  = Poseidon([rho, id_hash, 4243])                    # reserved; unused on chain

The shipped unified ``spend.circom`` derives the 4242-tagged nullifier for
EVERY flavor; the 4243 tag is reserved in case a future flavor-split
derivation is wanted (it would keep the namespaces disjoint for the same
``(rho, id_hash)`` pair).

For B1 only, the spend circuit also exposes the recomputed note commitment as
``issuanceCommitment``.  ``Notes`` records that commitment under the public
issuer whose registered-key batch Schnorr authorized its mint, and requires
the spend-time issuer to match.  A1/A2 keep this public signal at zero.

A-flavor identity binding: the spend circuit does **not** learn the
recipient from ``id_hash`` -- it is opaque to the circuit.  The addressed
(A1/A2) binding is enforced by the folded deposit gate at spend
(:file:`circuits/deposit_fold_a1.circom`, :file:`deposit_fold_a2.circom`),
which carries the note<->eEnc tie as one of its relations: it re-derives this
same nullifier in-circuit from ``(rho, id_hash)``, so the ciphertext it checks
is the spent note's.  Authorization keys on the recipient
*identity* ``m_rec``, not on any mint-time account key-pair, so key loss
is recoverable by binding a new account to the same identity.  (The
earlier account-pinned design -- an in-circuit Chaum-Pedersen equality on
the mint-time ``sk_rec``, making loss terminal -- is retired.)

``id_hash`` is the wallet's deterministic Poseidon-of-payload commitment to
the identity material, so two notes for the same recipient/issuer hash to the
same ``id_hash`` field element.  The ``id_hash_a1/a2/b1`` helpers compute it
from the canonical payload word layouts produced by ``id_payload_*``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from alberta_buck.wallet.bn254 import ORDER, point_to_words
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.poseidon import F_R, poseidon

# Domain-separation tags appearing as the third Poseidon input alongside
# (rho, id_hash).  A single change of tag yields a fully distinct output.
NULLIFIER_TAG_B = 4242
NULLIFIER_TAG_A = 4243

# Flavor labels -- match the circuit's public `flavor` input.
FLAVOR_A1 = 1
FLAVOR_A2 = 2
FLAVOR_B1 = 3

_FLAVORS = {FLAVOR_A1, FLAVOR_A2, FLAVOR_B1}


@dataclass(frozen=True)
class NoteOpening:
    """The witness a wallet stores for one outstanding note.

    Mirrors the SNARK opening tuple ``(flavor, v, rho, id_hash, predicate)``
    -- the five Poseidon-5 words in :file:`circuits/spend.circom` (``flavor``
    is also a public input bound to the entry-point mode).  The wallet is
    responsible for computing
    ``id_hash`` from the appropriate identity material via the
    :func:`id_hash_a1` / :func:`id_hash_a2` / :func:`id_hash_b1` helpers
    below; the dataclass treats it as an opaque field element.
    """
    flavor:    int
    v:         int
    rho:       int
    id_hash:   int = 0
    predicate: int = 0

    def __post_init__(self) -> None:
        if self.flavor not in _FLAVORS:
            raise ValueError(f"unknown flavor: {self.flavor}")
        if self.v < 0 or self.v.bit_length() > 128:
            raise ValueError("v out of range [0, 2^128)")
        if not (0 < self.rho < ORDER):
            raise ValueError("rho must be a non-zero scalar mod ORDER")
        if not (0 <= self.id_hash < F_R):
            raise ValueError("id_hash must lie in [0, F_R)")
        if not (0 <= self.predicate < F_R):
            raise ValueError("predicate must lie in [0, F_R)")


# ---- commitment -----------------------------------------------------------

def note_commitment(opening: NoteOpening) -> int:
    """``cm = Poseidon([flavor, v, rho, id_hash, predicate])``.

    Matches :file:`circuits/spend.circom` constraint (C) byte-for-byte.
    """
    return poseidon([
        opening.flavor,
        opening.v,
        opening.rho,
        opening.id_hash,
        opening.predicate,
    ])


# ---- nullifiers -----------------------------------------------------------

def nullifier_b(rho: int, id_hash: int) -> int:
    """B-spend nullifier: ``Poseidon([rho, id_hash, 4242])``.

    Matches :file:`circuits/spend.circom` constraint (N).  For B-flavor
    (bearer) notes the spend authorization is knowledge of ``rho``; the
    contract simply checks that the nullifier hasn't been seen before.
    """
    if not (0 < rho < ORDER):
        raise ValueError("rho must be a non-zero scalar mod ORDER")
    if not (0 <= id_hash < F_R):
        raise ValueError("id_hash must lie in [0, F_R)")
    return poseidon([rho, id_hash, NULLIFIER_TAG_B])


def nullifier_a(rho: int, id_hash: int) -> int:
    """RESERVED A-tag nullifier: ``Poseidon([rho, id_hash, 4243])``.

    NOT used by the shipped unified :file:`circuits/spend.circom`, which
    derives the 4242-tagged nullifier for every flavor; kept for a possible
    future flavor-split derivation (the distinct tag would make cross-flavor
    replay structurally impossible for the same ``(rho, id_hash)``).
    Identity binding is **not** carried by the nullifier either way -- it is
    enforced by the Identity-M deposit gate (coupling sigma + membership +
    the note<->eEnc binding SNARK); see the module docstring.
    """
    if not (0 < rho < ORDER):
        raise ValueError("rho must be a non-zero scalar mod ORDER")
    if not (0 <= id_hash < F_R):
        raise ValueError("id_hash must lie in [0, F_R)")
    return poseidon([rho, id_hash, NULLIFIER_TAG_A])


# ---- id_payload word encodings --------------------------------------------
# These return the canonical uint256 tuple that an off-chain witness blob
# would carry; id_hash_* below collapses each tuple to a single F_R element
# via Poseidon so the circuit only sees one private signal.

def id_payload_b1(m_issuer: int, sigma_R, sigma_s: int) -> Tuple[int, ...]:
    """B1 id-payload (bearer, public issuer): ``(m_issuer, sigma_R.x, sigma_R.y, sigma_s)``."""
    if not (0 < m_issuer < ORDER) or not (0 < sigma_s < ORDER):
        raise ValueError("scalars must lie in [1, ORDER)")
    return (m_issuer, *point_to_words(sigma_R), sigma_s)


def id_payload_a1(E_note: ElGamalCiphertext, m_issuer: int, sigma_R, sigma_s: int) -> Tuple[int, ...]:
    """A1 id-payload (addressed, public issuer): ``(E_note.R, E_note.C, m_issuer, sigma_R, sigma_s)``."""
    if not (0 < m_issuer < ORDER) or not (0 < sigma_s < ORDER):
        raise ValueError("scalars must lie in [1, ORDER)")
    return (*point_to_words(E_note.R), *point_to_words(E_note.C),
            m_issuer, *point_to_words(sigma_R), sigma_s)


def id_payload_a2(E_note: ElGamalCiphertext, E_issuer_for_rec: ElGamalCiphertext) -> Tuple[int, ...]:
    """A2 id-payload (addressed, private issuer): ``(E_note.R, E_note.C, E_iss.R, E_iss.C)``."""
    return (*point_to_words(E_note.R), *point_to_words(E_note.C),
            *point_to_words(E_issuer_for_rec.R), *point_to_words(E_issuer_for_rec.C))


# ---- id_hash helpers (Poseidon of payload) --------------------------------

def _hash_words(words: Tuple[int, ...]) -> int:
    """Poseidon over the payload, with each input reduced mod F_R first.

    BN254 base-field coords from ``point_to_words`` may exceed F_R (the
    scalar field).  Circom signals reduce automatically; we mirror that here
    so the Python id_hash matches the in-circuit hash bit-for-bit.
    """
    return poseidon([w % F_R for w in words])


def id_hash_b1(m_issuer: int, sigma_R, sigma_s: int) -> int:
    return _hash_words(id_payload_b1(m_issuer, sigma_R, sigma_s))


def id_hash_a1(E_note: ElGamalCiphertext, m_issuer: int, sigma_R, sigma_s: int) -> int:
    return _hash_words(id_payload_a1(E_note, m_issuer, sigma_R, sigma_s))


def id_hash_a2(E_note: ElGamalCiphertext, E_issuer_for_rec: ElGamalCiphertext) -> int:
    return _hash_words(id_payload_a2(E_note, E_issuer_for_rec))


__all__ = [
    "NoteOpening",
    "FLAVOR_A1", "FLAVOR_A2", "FLAVOR_B1",
    "NULLIFIER_TAG_A", "NULLIFIER_TAG_B",
    "note_commitment", "nullifier_a", "nullifier_b",
    "id_payload_a1", "id_payload_a2", "id_payload_b1",
    "id_hash_a1", "id_hash_a2", "id_hash_b1",
]
