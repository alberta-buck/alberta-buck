"""BUCK Notes commitment / nullifier construction (Phase 1 placeholders).

The Notes design (alberta-buck-notes.org) commits a note as a Poseidon hash
of (flavor, v, rho, id_payload, predicate); spend uses a Poseidon-keyed PRF
for the nullifier.  Phase 1 ships the on-chain note pool against a stub
SNARK verifier, so a real Poseidon implementation is not yet required.

This module provides:

  * The :class:`NoteOpening` dataclass: the canonical witness tuple a wallet
    holds for each note it owns or has issued.
  * Deterministic, collision-resistant *placeholder* commitment and nullifier
    functions implemented with keccak256.  These are used to produce vector
    fixtures and exercise the on-chain pool, but the Phase 2 SNARK toolchain
    will replace them with circomlib-compatible Poseidon.

The Solidity contract never recomputes either function on-chain (commitments
are opaque field elements; nullifiers are SNARK outputs), so the placeholder
choice has zero contract-side coupling.  When Poseidon lands the swap is
local to this file plus the test vectors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

from alberta_buck.wallet.bn254 import ORDER, point_to_words
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.transcript import keccak_bytes

# ---- domain separators ----------------------------------------------------
# Tags are short ASCII strings, hashed to a uint256 for inclusion in the
# packed transcript.  Stable across releases; future migrations bump the
# version suffix.

def _tag(s: str) -> int:
    digest = keccak_bytes(int.from_bytes(s.encode("utf-8").ljust(32, b"\0"), "big"))
    return int.from_bytes(digest, "big")


_TAG_CM   = _tag("BUCKNOTE/CM/v1")
_TAG_NF_A = _tag("BUCKNOTE/NF/A/v1")
_TAG_NF_B = _tag("BUCKNOTE/NF/B/v1")

FLAVOR_A1 = 1
FLAVOR_A2 = 2
FLAVOR_B1 = 3

_FLAVORS = {FLAVOR_A1, FLAVOR_A2, FLAVOR_B1}


@dataclass(frozen=True)
class NoteOpening:
    """The witness a wallet stores for one outstanding note.

    Fields mirror the SNARK opening tuple from
    alberta-buck-notes.org section "The Note Commitment".

    * ``flavor``     - one of FLAVOR_A1 / FLAVOR_A2 / FLAVOR_B1.
    * ``v``          - face value (uint, contract units).
    * ``rho``        - per-note randomness, source of the nullifier.
    * ``id_payload`` - flavor-specific identity material:
                         A1: (E_note, m_issuer, sigma)  [public issuer]
                         A2: (E_note, E_issuer_for_rec) [private issuer]
                         B1: (m_issuer, sigma)          [bearer, public issuer]
                       Phase 1 keeps this opaque -- callers serialize their
                       own canonical encoding via ``id_payload_words``.
    * ``predicate``  - optional spend predicate hash (zero == "no predicate").
    """
    flavor:               int
    v:                    int
    rho:                  int
    id_payload_words:     Tuple[int, ...] = field(default_factory=tuple)
    predicate:            int             = 0

    def __post_init__(self) -> None:
        if self.flavor not in _FLAVORS:
            raise ValueError(f"unknown flavor: {self.flavor}")
        if self.v < 0 or self.v.bit_length() > 256:
            raise ValueError("v out of range")
        if not (0 < self.rho < ORDER):
            raise ValueError("rho must be a non-zero scalar mod ORDER")
        for w in self.id_payload_words:
            if not isinstance(w, int) or w < 0 or w.bit_length() > 256:
                raise ValueError("id_payload_words must be uint256s")
        if self.predicate < 0 or self.predicate.bit_length() > 256:
            raise ValueError("predicate must be a uint256")


# ---- commitment -----------------------------------------------------------

def note_commitment(opening: NoteOpening) -> int:
    """Phase 1 placeholder for ``H_cm(flavor, v, rho, id_payload, predicate)``.

    Uses keccak256 with a domain-separating tag and a length prefix on the
    id-payload, reduced mod ORDER so the result lives in the BN254 scalar
    field (the field every Phase 2 Poseidon variant will hash into).

    Replaced wholesale in Phase 2 by a Poseidon hash matching the chosen
    SNARK toolchain.  The function signature is stable; only the hash
    function body changes.
    """
    pad = [opening.flavor, opening.v, opening.rho,
           len(opening.id_payload_words), *opening.id_payload_words,
           opening.predicate]
    digest = keccak_bytes(*pad)
    final = keccak_bytes(_TAG_CM, int.from_bytes(digest, "big"))
    return int.from_bytes(final, "big") % ORDER


# ---- nullifier ------------------------------------------------------------

def nullifier_a(M_rec, rho: int) -> int:
    """A-spend nullifier: H_nf(A || M_rec || rho).

    ``M_rec`` is a G1 point (the recipient's identity point M).  Keying on
    M -- not on sk_dep -- is what makes key-loss recovery work.  Phase 2
    swaps keccak256 for Poseidon.
    """
    if not (0 < rho < ORDER):
        raise ValueError("rho must be a non-zero scalar mod ORDER")
    Mx, My = point_to_words(M_rec)
    digest = keccak_bytes(_TAG_NF_A, Mx, My, rho)
    return int.from_bytes(digest, "big") % ORDER


def nullifier_b(rho: int) -> int:
    """B-spend nullifier: H_nf(B || rho).  Publicly derivable from rho."""
    if not (0 < rho < ORDER):
        raise ValueError("rho must be a non-zero scalar mod ORDER")
    digest = keccak_bytes(_TAG_NF_B, rho)
    return int.from_bytes(digest, "big") % ORDER


# ---- helpers --------------------------------------------------------------

def id_payload_a2(E_note: ElGamalCiphertext, E_issuer_for_rec: ElGamalCiphertext) -> Tuple[int, ...]:
    """Canonical word encoding of an A2 id-payload.

    Order: (E_note.R.x, E_note.R.y, E_note.C.x, E_note.C.y,
            E_issuer.R.x, E_issuer.R.y, E_issuer.C.x, E_issuer.C.y).
    """
    return (*point_to_words(E_note.R), *point_to_words(E_note.C),
            *point_to_words(E_issuer_for_rec.R), *point_to_words(E_issuer_for_rec.C))


def id_payload_a1(E_note: ElGamalCiphertext, m_issuer: int, sigma_R, sigma_s: int) -> Tuple[int, ...]:
    """Canonical word encoding of an A1 id-payload (public issuer + Schnorr sig)."""
    if not (0 < m_issuer < ORDER) or not (0 < sigma_s < ORDER):
        raise ValueError("scalars must lie in [1, ORDER)")
    return (*point_to_words(E_note.R), *point_to_words(E_note.C),
            m_issuer, *point_to_words(sigma_R), sigma_s)


def id_payload_b1(m_issuer: int, sigma_R, sigma_s: int) -> Tuple[int, ...]:
    """Canonical word encoding of a B1 id-payload (bearer, public issuer)."""
    if not (0 < m_issuer < ORDER) or not (0 < sigma_s < ORDER):
        raise ValueError("scalars must lie in [1, ORDER)")
    return (m_issuer, *point_to_words(sigma_R), sigma_s)


__all__ = [
    "NoteOpening",
    "FLAVOR_A1", "FLAVOR_A2", "FLAVOR_B1",
    "note_commitment", "nullifier_a", "nullifier_b",
    "id_payload_a1", "id_payload_a2", "id_payload_b1",
]
