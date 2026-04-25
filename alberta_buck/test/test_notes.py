"""Unit tests for alberta_buck.wallet.notes (Phase 7+ corrected design).

Verifies that the wallet's note_commitment and nullifier_{a,b} agree with
the shipped spend.circom and the planned spend_a.circom: Poseidon-5
commitment, Poseidon-3 nullifier with domain-separating tags 4242 (B) /
4243 (A).  The Poseidon implementation itself is checked against
circomlibjs vectors in test_poseidon.py.
"""

from __future__ import annotations

import pytest

from alberta_buck.wallet import (
    G1, ORDER, mul, F_R, poseidon,
    FLAVOR_A1, FLAVOR_A2, FLAVOR_B1, NoteOpening,
    NULLIFIER_TAG_A, NULLIFIER_TAG_B,
    note_commitment, nullifier_a, nullifier_b,
    id_payload_a1, id_payload_a2, id_payload_b1,
    id_hash_a1, id_hash_a2, id_hash_b1,
    elgamal_encrypt, identity_keygen,
)
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


def test_commitment_matches_poseidon5_directly():
    """note_commitment IS Poseidon([flavor, v, rho, id_hash, predicate])."""
    o = _opening()
    assert note_commitment(o) == poseidon([o.flavor, o.v, o.rho, o.id_hash, o.predicate])


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

def test_nullifier_b_matches_poseidon3_with_tag_4242():
    rho, idh = 0xC0FFEE, 0xBEEF
    assert nullifier_b(rho, idh) == poseidon([rho, idh, NULLIFIER_TAG_B])
    assert NULLIFIER_TAG_B == 4242


def test_nullifier_a_matches_poseidon3_with_tag_4243():
    rho, idh = 0xC0FFEE, 0xBEEF
    assert nullifier_a(rho, idh) == poseidon([rho, idh, NULLIFIER_TAG_A])
    assert NULLIFIER_TAG_A == 4243


def test_nullifier_a_and_b_disjoint_for_same_inputs():
    """Different tag => different hash, so cross-flavor replay is impossible."""
    rho, idh = 0xC0FFEE, 0xBEEF
    assert nullifier_a(rho, idh) != nullifier_b(rho, idh)


def test_nullifier_changes_with_id_hash():
    rho = 0xC0FFEE
    assert nullifier_a(rho, 1) != nullifier_a(rho, 2)
    assert nullifier_b(rho, 1) != nullifier_b(rho, 2)


def test_nullifier_changes_with_rho():
    idh = 0xBEEF
    assert nullifier_a(1, idh) != nullifier_a(2, idh)
    assert nullifier_b(1, idh) != nullifier_b(2, idh)


def test_nullifier_rejects_zero_rho():
    with pytest.raises(ValueError):
        nullifier_a(0, 1)
    with pytest.raises(ValueError):
        nullifier_b(0, 1)


def test_nullifier_rejects_id_hash_outside_field():
    with pytest.raises(ValueError):
        nullifier_a(1, F_R)
    with pytest.raises(ValueError):
        nullifier_b(1, F_R)


# --- id_payload helpers (canonical word layout) ---------------------------

def _make_ct():
    kp = identity_keygen()
    return elgamal_encrypt(mul(G1, 7), kp.pk, 11)


def test_id_payload_a2_layout():
    E_note = _make_ct()
    E_iss  = _make_ct()
    words = id_payload_a2(E_note, E_iss)
    assert len(words) == 8
    expected = (*point_to_words(E_note.R), *point_to_words(E_note.C),
                *point_to_words(E_iss.R),  *point_to_words(E_iss.C))
    assert words == expected


def test_id_payload_a1_layout():
    E_note  = _make_ct()
    sigma_R = mul(G1, 3)
    words   = id_payload_a1(E_note, m_issuer=5, sigma_R=sigma_R, sigma_s=9)
    assert len(words) == 8  # E_note(4) + m_iss(1) + sigma_R(2) + sigma_s(1)


def test_id_payload_b1_layout():
    sigma_R = mul(G1, 3)
    words   = id_payload_b1(m_issuer=5, sigma_R=sigma_R, sigma_s=9)
    assert len(words) == 4  # m_iss(1) + sigma_R(2) + sigma_s(1)


# --- id_hash helpers ------------------------------------------------------

def test_id_hash_b1_collapses_to_single_field_element():
    sigma_R = mul(G1, 3)
    h = id_hash_b1(m_issuer=5, sigma_R=sigma_R, sigma_s=9)
    assert 0 <= h < F_R


def test_id_hash_a1_a2_collapse_to_single_field_element():
    E_note = _make_ct()
    E_iss  = _make_ct()
    sigma_R = mul(G1, 3)
    h_a1 = id_hash_a1(E_note, m_issuer=5, sigma_R=sigma_R, sigma_s=9)
    h_a2 = id_hash_a2(E_note, E_iss)
    assert 0 <= h_a1 < F_R
    assert 0 <= h_a2 < F_R
    # Different payload shapes hashing the same E_note must not collide.
    assert h_a1 != h_a2


def test_id_hash_b1_deterministic_in_inputs():
    sigma_R = mul(G1, 3)
    a = id_hash_b1(m_issuer=5, sigma_R=sigma_R, sigma_s=9)
    b = id_hash_b1(m_issuer=5, sigma_R=sigma_R, sigma_s=9)
    assert a == b
    # Single-field change perturbs the hash.
    c = id_hash_b1(m_issuer=6, sigma_R=sigma_R, sigma_s=9)
    assert a != c


def test_id_hash_matches_poseidon_of_payload_words():
    """The id_hash helpers ARE Poseidon over the canonical payload words mod F_R."""
    sigma_R = mul(G1, 3)
    pay = id_payload_b1(m_issuer=5, sigma_R=sigma_R, sigma_s=9)
    assert id_hash_b1(m_issuer=5, sigma_R=sigma_R, sigma_s=9) == \
           poseidon([w % F_R for w in pay])
