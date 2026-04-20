"""Unit tests for alberta_buck.wallet.notes (Phase 1 placeholder primitives)."""

from __future__ import annotations

import pytest

from alberta_buck.wallet import (
    G1, ORDER, mul,
    FLAVOR_A1, FLAVOR_A2, FLAVOR_B1, NoteOpening,
    note_commitment, nullifier_a, nullifier_b,
    id_payload_a1, id_payload_a2, id_payload_b1,
    elgamal_encrypt, identity_keygen,
)


# --- NoteOpening validation -----------------------------------------------

def test_opening_rejects_unknown_flavor():
    with pytest.raises(ValueError, match="flavor"):
        NoteOpening(flavor=99, v=1, rho=1)


def test_opening_rejects_zero_rho():
    with pytest.raises(ValueError, match="rho"):
        NoteOpening(flavor=FLAVOR_A2, v=1, rho=0)


def test_opening_rejects_oversized_v():
    with pytest.raises(ValueError):
        NoteOpening(flavor=FLAVOR_A2, v=1 << 256, rho=1)


def test_opening_rejects_non_uint_payload():
    with pytest.raises(ValueError):
        NoteOpening(
            flavor=FLAVOR_A2, v=1, rho=1,
            id_payload_words=(1, -2, 3),
        )


# --- commitment determinism + sensitivity ---------------------------------

def _opening(rho_offset: int = 0, v: int = 1_000_000_000_000_000_000) -> NoteOpening:
    return NoteOpening(
        flavor=FLAVOR_A2, v=v, rho=42 + rho_offset,
        id_payload_words=(0xDEAD, 0xBEEF, 0xCAFE, 0xF00D),
        predicate=0,
    )


def test_commitment_is_deterministic():
    a = note_commitment(_opening())
    b = note_commitment(_opening())
    assert a == b
    assert 0 < a < ORDER


def test_commitment_changes_with_each_field():
    base = note_commitment(_opening())
    diff_rho   = note_commitment(_opening(rho_offset=1))
    diff_v     = note_commitment(_opening(v=99))
    diff_flav  = note_commitment(NoteOpening(
        flavor=FLAVOR_A1, v=1_000_000_000_000_000_000, rho=42,
        id_payload_words=(0xDEAD, 0xBEEF, 0xCAFE, 0xF00D),
    ))
    diff_pred  = note_commitment(NoteOpening(
        flavor=FLAVOR_A2, v=1_000_000_000_000_000_000, rho=42,
        id_payload_words=(0xDEAD, 0xBEEF, 0xCAFE, 0xF00D),
        predicate=1,
    ))
    diff_pay   = note_commitment(NoteOpening(
        flavor=FLAVOR_A2, v=1_000_000_000_000_000_000, rho=42,
        id_payload_words=(0xDEAD, 0xBEEF, 0xCAFE, 0xF00E),
    ))
    seen = {base, diff_rho, diff_v, diff_flav, diff_pred, diff_pay}
    assert len(seen) == 6, "every field must change the commitment"


# --- nullifier domain separation ------------------------------------------

def test_nullifier_a_and_b_disjoint_for_same_rho():
    rho = 0xC0FFEE
    M = mul(G1, 12345)
    nf_a = nullifier_a(M, rho)
    nf_b = nullifier_b(rho)
    assert nf_a != nf_b


def test_nullifier_a_keys_on_M():
    rho = 0xC0FFEE
    nf1 = nullifier_a(mul(G1, 1), rho)
    nf2 = nullifier_a(mul(G1, 2), rho)
    assert nf1 != nf2


def test_nullifier_b_deterministic_in_rho():
    rho = 0x1234567890ABCDEF
    assert nullifier_b(rho) == nullifier_b(rho)


def test_nullifier_rejects_zero_rho():
    with pytest.raises(ValueError):
        nullifier_a(mul(G1, 1), 0)
    with pytest.raises(ValueError):
        nullifier_b(0)


# --- id_payload helpers ---------------------------------------------------

def _make_ct():
    kp = identity_keygen()
    return elgamal_encrypt(mul(G1, 7), kp.pk, 11)


def test_id_payload_a2_layout():
    E_note = _make_ct()
    E_iss  = _make_ct()
    words = id_payload_a2(E_note, E_iss)
    assert len(words) == 8
    # Reconstruct expected layout from public coords.
    from alberta_buck.wallet.bn254 import point_to_words
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
