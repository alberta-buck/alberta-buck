"""BUCK Notes commitment / nullifier construction (Phase 7+ corrected design).

Hash family: circomlib Poseidon over BN254 (matches the shipped
=spend.circom=).  The Python implementation
in :mod:`alberta_buck.wallet.poseidon` agrees with circomlibjs's unoptimized
variant, which is the same hash the optimized circuit Poseidon computes (just
via different but equivalent constants).  See :file:`scripts/snark/poseidon_t3_code.js`
for the on-chain bytecode story.

Wire formats, each Poseidon led by its v2 domain tag ``T = keccak(tag) mod F_R``
(:mod:`alberta_buck.wallet.domains`)::

    cm      = Poseidon([T_CM, flavor, v, rho, id_hash, predicate])   # spend.circom (C)
    nf      = Poseidon([T_NF, rho, id_hash])                         # spend.circom (N)
    id_hash = Poseidon([T_ID, *payload])                             # per flavour, below

One nullifier serves every flavour: the spend circuit derives it the same way
for A1, A2 and B1.

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
the identity material.  The ``id_hash_a1/a2/b1`` helpers compute it from the
canonical payload word layouts produced by ``id_payload_*``: the public
issuer's identity scalar for B1; the value ciphertext and that scalar for A1;
the value and issuer ciphertexts and the key tie ``T`` for A2.  A public
issuer is authenticated by the batch Schnorr its account signs at mint, which
covers each exact commitment; the note carries no signature of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from alberta_buck.wallet.bn254 import ORDER, point_to_words
from alberta_buck.wallet.domains import NOTES_COMMITMENT, NOTES_ID_HASH, NOTES_NULLIFIER, field_tag
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.poseidon import F_R, poseidon

# The note hashes' leading Poseidon inputs (compiled into the circuits).
TAG_COMMITMENT                  = field_tag(NOTES_COMMITMENT)
TAG_NULLIFIER                   = field_tag(NOTES_NULLIFIER)
TAG_ID_HASH                     = field_tag(NOTES_ID_HASH)

# Flavor labels -- match the circuit's public `flavor` input.
FLAVOR_A1 = 1
FLAVOR_A2 = 2
FLAVOR_B1 = 3

_FLAVORS = {FLAVOR_A1, FLAVOR_A2, FLAVOR_B1}


@dataclass(frozen=True)
class NoteOpening:
    """The witness a wallet stores for one outstanding note.

    Mirrors the SNARK opening tuple ``(flavor, v, rho, id_hash, predicate)``
    -- the five opened words in :file:`circuits/spend.circom` (``flavor``
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
    """``cm = Poseidon([T_CM, flavor, v, rho, id_hash, predicate])``.

    Matches :file:`circuits/spend.circom` constraint (C) byte-for-byte.
    """
    return poseidon([
        TAG_COMMITMENT,
        opening.flavor,
        opening.v,
        opening.rho,
        opening.id_hash,
        opening.predicate,
    ])


# ---- nullifiers -----------------------------------------------------------

def nullifier(rho: int, id_hash: int) -> int:
    """The spent marker: ``Poseidon([T_NF, rho, id_hash])``, for every flavour.

    Matches :file:`circuits/spend.circom` constraint (N) and the deposit folds'
    re-derivation.  Knowledge of ``rho`` is what spends; the contract refuses a
    nullifier it has seen, and identity binding is the deposit gate's.
    """
    if not (0 < rho < ORDER):
        raise ValueError("rho must be a non-zero scalar mod ORDER")
    if not (0 <= id_hash < F_R):
        raise ValueError("id_hash must lie in [0, F_R)")
    return poseidon([TAG_NULLIFIER, rho, id_hash])


# ---- id_payload word encodings --------------------------------------------
# These return the canonical uint256 tuple that an off-chain witness blob
# would carry; id_hash_* below collapses each tuple to a single F_R element
# via Poseidon so the circuit only sees one private signal.

def id_payload_b1(m_issuer: int) -> Tuple[int, ...]:
    """B1 id-payload (bearer, public issuer): ``(m_issuer,)``."""
    if not (0 < m_issuer < ORDER):
        raise ValueError("m_issuer must lie in [1, ORDER)")
    return (m_issuer,)


def id_payload_a1(E_note: ElGamalCiphertext, m_issuer: int) -> Tuple[int, ...]:
    """A1 id-payload (addressed, public issuer): ``(E_note.R, E_note.C, m_issuer)``."""
    if not (0 < m_issuer < ORDER):
        raise ValueError("m_issuer must lie in [1, ORDER)")
    return (*point_to_words(E_note.R), *point_to_words(E_note.C), m_issuer)


def id_payload_a2(E_note: ElGamalCiphertext, E_issuer_for_rec: ElGamalCiphertext, T) -> Tuple[int, ...]:
    """A2 id-payload (addressed, private issuer): ``(E_note.R, E_note.C, E_iss.R, E_iss.C, T)``.

    ``T = r'*pk_recv + gamma*H`` is the mint binding's blinded point.  The spend can reach the mint
    only through ``idHash``, so committing ``T`` is what lets the A2 fold tie the key the binding
    proved about to the recipient's own (doc/review/notes-receiving-key.org, section 4.6).
    """
    return (*point_to_words(E_note.R), *point_to_words(E_note.C),
            *point_to_words(E_issuer_for_rec.R), *point_to_words(E_issuer_for_rec.C),
            *point_to_words(T))


# ---- id_hash helpers (Poseidon of payload) --------------------------------

def _hash_words(words: Tuple[int, ...]) -> int:
    """``Poseidon([T_ID, *payload])``, each payload word reduced mod F_R first.

    BN254 base-field coords from ``point_to_words`` may exceed F_R (the
    scalar field).  Circom signals reduce automatically; we mirror that here
    so the Python id_hash matches the in-circuit hash bit-for-bit.
    """
    return poseidon([TAG_ID_HASH] + [w % F_R for w in words])


def id_hash_b1(m_issuer: int) -> int:
    return _hash_words(id_payload_b1(m_issuer))


def id_hash_a1(E_note: ElGamalCiphertext, m_issuer: int) -> int:
    return _hash_words(id_payload_a1(E_note, m_issuer))


def id_hash_a2(E_note: ElGamalCiphertext, E_issuer_for_rec: ElGamalCiphertext, T) -> int:
    return _hash_words(id_payload_a2(E_note, E_issuer_for_rec, T))


__all__ = [
    "NoteOpening",
    "FLAVOR_A1", "FLAVOR_A2", "FLAVOR_B1",
    "TAG_COMMITMENT", "TAG_ID_HASH", "TAG_NULLIFIER",
    "note_commitment", "nullifier",
    "id_payload_a1", "id_payload_a2", "id_payload_b1",
    "id_hash_a1", "id_hash_a2", "id_hash_b1",
]
