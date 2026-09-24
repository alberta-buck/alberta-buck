"""Unit tests for alberta_buck.wallet.notes.

The wallet's note_commitment, nullifier and id_hash_* agree with the shipped
circuits: each a Poseidon led by its v2 domain tag (keccak(tag) mod F_R from
alberta_buck.wallet.domains), so the commitment, the spent marker and the
identity hash are three distinct functions.  The Poseidon implementation itself
is checked against circomlibjs vectors in test_poseidon.py.
"""

from __future__ import annotations

import pytest

from alberta_buck.wallet import (
    G1, ORDER, mul, F_R, poseidon,
    FLAVOR_A1, FLAVOR_A2, FLAVOR_B1, NoteOpening,
    TAG_COMMITMENT, TAG_ID_HASH, TAG_NULLIFIER,
    note_commitment, nullifier,
    id_payload_a1, id_payload_a2, id_payload_b1,
    id_hash_a1, id_hash_a2, id_hash_b1,
    elgamal_encrypt, identity_keygen,
)
from alberta_buck.wallet import domains
from alberta_buck.wallet.bn254 import point_to_words


# --- NoteOpening validation -----------------------------------------------

def test_opening_rejects_unknown_flavor():
    with pytest.raises(ValueError, match="flavor"):
        NoteOpening(flavor=99, v=1, rho=1)


def test_opening_rejects_zero_rho():
    with pytest.raises(ValueError, match="rho"):
        NoteOpening(flavor=FLAVOR_A2, v=1, rho=0)


def test_opening_rejects_oversized_v():
    with pytest.raises(ValueError):
        NoteOpening(flavor=FLAVOR_A2, v=1 << 128, rho=1)


def test_opening_rejects_id_hash_outside_field():
    with pytest.raises(ValueError):
        NoteOpening(flavor=FLAVOR_A2, v=1, rho=1, id_hash=F_R)


# --- commitment exact match against the circuit's hash --------------------

def _opening(rho_offset: int = 0, v: int = 1_000_000_000_000_000_000,
             id_hash: int = 0xDEADBEEF, predicate: int = 0,
             flavor: int = FLAVOR_A2) -> NoteOpening:
    return NoteOpening(
        flavor=flavor, v=v, rho=42 + rho_offset,
        id_hash=id_hash, predicate=predicate,
    )


def test_note_tags_are_the_registry_tags():
    assert TAG_COMMITMENT == domains.field_tag(domains.NOTES_COMMITMENT)
    assert TAG_NULLIFIER == domains.field_tag(domains.NOTES_NULLIFIER)
    assert TAG_ID_HASH == domains.field_tag(domains.NOTES_ID_HASH)
    assert len({TAG_COMMITMENT, TAG_NULLIFIER, TAG_ID_HASH}) == 3


def test_commitment_matches_tagged_poseidon6_directly():
    """note_commitment IS Poseidon([T_CM, flavor, v, rho, id_hash, predicate])."""
    o = _opening()
    assert note_commitment(o) == poseidon([TAG_COMMITMENT, o.flavor, o.v, o.rho, o.id_hash, o.predicate])


def test_commitment_is_deterministic():
    a = note_commitment(_opening())
    b = note_commitment(_opening())
    assert a == b
    assert 0 < a < F_R


def test_commitment_changes_with_each_field():
    base       = note_commitment(_opening())
    diff_rho   = note_commitment(_opening(rho_offset=1))
    diff_v     = note_commitment(_opening(v=99))
    diff_flav  = note_commitment(_opening(flavor=FLAVOR_A1))
    diff_pred  = note_commitment(_opening(predicate=1))
    diff_idh   = note_commitment(_opening(id_hash=0xCAFEF00D))
    seen = {base, diff_rho, diff_v, diff_flav, diff_pred, diff_idh}
    assert len(seen) == 6, "every field must change the commitment"


# --- nullifier exact match + domain separation ----------------------------

def test_nullifier_matches_tagged_poseidon3():
    rho, idh = 0xC0FFEE, 0xBEEF
    assert nullifier(rho, idh) == poseidon([TAG_NULLIFIER, rho, idh])


def test_nullifier_changes_with_id_hash():
    assert nullifier(0xC0FFEE, 1) != nullifier(0xC0FFEE, 2)


def test_nullifier_changes_with_rho():
    assert nullifier(1, 0xBEEF) != nullifier(2, 0xBEEF)


def test_nullifier_rejects_zero_rho():
    with pytest.raises(ValueError):
        nullifier(0, 1)


def test_nullifier_rejects_id_hash_outside_field():
    with pytest.raises(ValueError):
        nullifier(1, F_R)


# --- id_payload helpers (canonical word layout) ---------------------------

def _make_ct():
    kp = identity_keygen()
    return elgamal_encrypt(mul(G1, 7), kp.pk, 11)


def test_id_payload_a2_layout():
    E_note = _make_ct()
    E_iss  = _make_ct()
    T      = mul(G1, 13)
    words = id_payload_a2(E_note, E_iss, T)
    assert len(words) == 10  # E_note(4) + E_iss(4) + T(2)
    expected = (*point_to_words(E_note.R), *point_to_words(E_note.C),
                *point_to_words(E_iss.R),  *point_to_words(E_iss.C),
                *point_to_words(T))
    assert words == expected


def test_id_payload_a1_layout():
    E_note = _make_ct()
    words  = id_payload_a1(E_note, m_issuer=5)
    assert words == (*point_to_words(E_note.R), *point_to_words(E_note.C), 5)


def test_id_payload_b1_layout():
    assert id_payload_b1(m_issuer=5) == (5,)
    with pytest.raises(ValueError):
        id_payload_b1(m_issuer=0)


# --- id_hash helpers ------------------------------------------------------

def test_id_hash_b1_collapses_to_single_field_element():
    h = id_hash_b1(m_issuer=5)
    assert 0 <= h < F_R


def test_id_hash_a1_a2_collapse_to_single_field_element():
    E_note = _make_ct()
    E_iss  = _make_ct()
    h_a1 = id_hash_a1(E_note, m_issuer=5)
    h_a2 = id_hash_a2(E_note, E_iss, mul(G1, 13))
    assert 0 <= h_a1 < F_R
    assert 0 <= h_a2 < F_R
    # Different payload shapes hashing the same E_note must not collide.
    assert h_a1 != h_a2


def test_id_hash_b1_deterministic_in_inputs():
    assert id_hash_b1(m_issuer=5) == id_hash_b1(m_issuer=5)
    assert id_hash_b1(m_issuer=5) != id_hash_b1(m_issuer=6)


def test_id_hash_matches_tagged_poseidon_of_payload_words():
    """The id_hash helpers ARE Poseidon over [T_ID, *payload words mod F_R]."""
    E_note = _make_ct()
    assert id_hash_b1(m_issuer=5) == poseidon([TAG_ID_HASH, 5])
    pay = id_payload_a1(E_note, m_issuer=5)
    assert id_hash_a1(E_note, m_issuer=5) == poseidon([TAG_ID_HASH] + [w % F_R for w in pay])


def test_the_three_note_hashes_are_distinct_functions():
    """Same-arity inputs never collide across uses: the leading tags differ."""
    words = [1, 2, 3, 4, 5]
    assert poseidon([TAG_COMMITMENT] + words) != poseidon([TAG_ID_HASH] + words)
    assert nullifier(1, 2) != poseidon([TAG_ID_HASH, 1, 2])
