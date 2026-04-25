"""Tests for the simulated government / institutional credential issuer.

Exercises the issuance ceremony in isolation, then composes it with the
existing wallet primitives (rerandomize -> ElGamal -> registration NIZK)
to confirm an issued credential rides cleanly through the full register()
path.
"""

from __future__ import annotations

import random

import pytest

from alberta_buck.wallet import (
    G1, ORDER, eq, mul,
    elgamal_decrypt, elgamal_encrypt, identity_keygen,
    identity_scalar, canonical_identity_data,
    ps_verify,
    registration_prove, registration_verify,
    Issuer, IssuedCredential, rerandomize_for_registration,
)
from alberta_buck.wallet.bn254 import rand_scalar


def seeded_rng(seed: int):
    rnd = random.Random(seed)
    return lambda: rnd.getrandbits(256)


ATB_ADDR  = 0xa7b00000000000000000000000000000000a7b00
SVCAB_ADDR = 0x5e2ce00000000000000000000000000005e2ce00

ALICE_ADDR = 0xa11ce00000000000000000000000000000a11ce
BOB_ADDR   = 0x0b0b000000000000000000000000000000000b0b


ALICE_FIELDS = {
    "given_name":    "Alice",
    "family_name":   "Johnson",
    "jurisdiction":  "Alberta, Canada",
    "id_type":       "Alberta Identity Card",
    "id_number":     "AIC-2026-4839201",
    "date_of_birth": "1992-03-15",
    "issued_at":     "2026-01-20T14:30:00Z",
    "epoch":         42,
}

BOB_FIELDS = {
    "given_name":    "Bob",
    "family_name":   "Smith",
    "jurisdiction":  "Alberta, Canada",
    "id_type":       "Corporate Registration",
    "id_number":     "AB-CORP-2026-00182",
    "date_of_birth": "1985-07-22",
    "issued_at":     "2026-02-01T09:00:00Z",
    "epoch":         42,
}


# --- Issuer setup ---------------------------------------------------------------

def test_issuer_setup_is_deterministic_for_same_seed():
    a = Issuer.setup("atb-financial-ca", ATB_ADDR, rng=seeded_rng(1))
    b = Issuer.setup("atb-financial-ca", ATB_ADDR, rng=seeded_rng(1))
    assert a.keypair.sk_x == b.keypair.sk_x
    assert a.keypair.sk_y == b.keypair.sk_y


def test_issuer_setup_differs_for_different_seed():
    a = Issuer.setup("atb-financial-ca", ATB_ADDR, rng=seeded_rng(1))
    b = Issuer.setup("atb-financial-ca", ATB_ADDR, rng=seeded_rng(2))
    assert a.keypair.sk_x != b.keypair.sk_x


# --- Single-credential issuance round-trip --------------------------------------

def test_issue_returns_credential_with_valid_signature():
    rng = seeded_rng(10)
    issuer = Issuer.setup("atb-financial-ca", ATB_ADDR, rng=rng)
    cred = issuer.issue(ALICE_FIELDS, ALICE_ADDR, rng=rng)
    assert isinstance(cred, IssuedCredential)
    assert cred.issuer_id == "atb-financial-ca"
    assert cred.issuer_addr == ATB_ADDR
    assert ps_verify(issuer.pk_X, issuer.pk_Y, cred.sigma, cred.m)


def test_issue_overwrites_issuer_id_in_record():
    """An applicant cannot smuggle a different issuer_id into the canonical."""
    rng = seeded_rng(11)
    issuer = Issuer.setup("atb-financial-ca", ATB_ADDR, rng=rng)
    bad = dict(ALICE_FIELDS, issuer_id="some-other-issuer")
    cred = issuer.issue(bad, ALICE_ADDR, rng=rng)
    expected = canonical_identity_data(dict(ALICE_FIELDS, issuer_id="atb-financial-ca"))
    assert cred.canonical == expected
    assert cred.m == identity_scalar(expected)


def test_issuer_verify_credential_accepts_own_signature():
    rng = seeded_rng(12)
    issuer = Issuer.setup("atb-financial-ca", ATB_ADDR, rng=rng)
    cred = issuer.issue(ALICE_FIELDS, ALICE_ADDR, rng=rng)
    assert issuer.verify_credential(cred)


def test_issuer_verify_credential_rejects_other_issuer_label():
    """A credential labeled for a different issuer doesn't match this one."""
    rng = seeded_rng(13)
    atb = Issuer.setup("atb-financial-ca", ATB_ADDR, rng=rng)
    other = Issuer.setup("service-alberta",   SVCAB_ADDR, rng=rng)
    cred_other = other.issue(ALICE_FIELDS, ALICE_ADDR, rng=rng)
    assert not atb.verify_credential(cred_other)


# --- Confidential delivery via ElGamal ------------------------------------------

def test_issue_with_applicant_pk_returns_encrypted_M():
    rng = seeded_rng(20)
    issuer = Issuer.setup("atb-financial-ca", ATB_ADDR, rng=rng)
    applicant_kp = identity_keygen(rng=rng)
    cred = issuer.issue(ALICE_FIELDS, ALICE_ADDR, applicant_pk=applicant_kp.pk, rng=rng)
    assert cred.delivery is not None
    M_dec = elgamal_decrypt(cred.delivery, applicant_kp.sk)
    assert eq(M_dec, mul(G1, cred.m))


def test_issue_without_applicant_pk_omits_delivery():
    rng = seeded_rng(21)
    issuer = Issuer.setup("atb-financial-ca", ATB_ADDR, rng=rng)
    cred = issuer.issue(ALICE_FIELDS, ALICE_ADDR, rng=rng)
    assert cred.delivery is None


# --- Revocation -----------------------------------------------------------------

def test_revoked_applicant_cannot_be_reissued():
    rng = seeded_rng(30)
    issuer = Issuer.setup("atb-financial-ca", ATB_ADDR, rng=rng)
    issuer.issue(ALICE_FIELDS, ALICE_ADDR, rng=rng)
    issuer.revoke(ALICE_ADDR)
    with pytest.raises(ValueError):
        issuer.issue(ALICE_FIELDS, ALICE_ADDR, rng=rng)


def test_reset_re_enables_issuance():
    rng = seeded_rng(31)
    issuer = Issuer.setup("atb-financial-ca", ATB_ADDR, rng=rng)
    issuer.revoke(ALICE_ADDR)
    issuer.reset(ALICE_ADDR)
    cred = issuer.issue(ALICE_FIELDS, ALICE_ADDR, rng=rng)
    assert ps_verify(issuer.pk_X, issuer.pk_Y, cred.sigma, cred.m)


def test_issuance_log_records_each_issue():
    rng = seeded_rng(32)
    issuer = Issuer.setup("atb-financial-ca", ATB_ADDR, rng=rng)
    issuer.issue(ALICE_FIELDS, ALICE_ADDR, rng=rng)
    issuer.issue(BOB_FIELDS,   BOB_ADDR,   rng=rng)
    log = issuer.issuance_log()
    assert len(log) == 2
    assert {e.applicant_addr for e in log} == {ALICE_ADDR, BOB_ADDR}
    # Mutating the returned copy doesn't poison the issuer's internal log.
    log.clear()
    assert len(issuer.issuance_log()) == 2


# --- Multi-issuer scenarios -----------------------------------------------------

def test_multiple_issuers_each_have_distinct_keys():
    rng_a = seeded_rng(40)
    rng_b = seeded_rng(41)
    atb   = Issuer.setup("atb-financial-ca", ATB_ADDR,  rng=rng_a)
    svcab = Issuer.setup("service-alberta",   SVCAB_ADDR, rng=rng_b)
    assert atb.keypair.sk_x != svcab.keypair.sk_x
    assert atb.issuer_addr != svcab.issuer_addr


def test_credential_issued_by_one_does_not_verify_under_other():
    rng = seeded_rng(42)
    atb   = Issuer.setup("atb-financial-ca", ATB_ADDR,  rng=rng)
    svcab = Issuer.setup("service-alberta",   SVCAB_ADDR, rng=rng)
    cred = atb.issue(ALICE_FIELDS, ALICE_ADDR, rng=rng)
    assert ps_verify(atb.pk_X,   atb.pk_Y,   cred.sigma, cred.m)
    assert not ps_verify(svcab.pk_X, svcab.pk_Y, cred.sigma, cred.m)


# --- End-to-end: issuance -> wallet rerandomization -> registration NIZK --------

def test_end_to_end_issuance_through_registration_nizk():
    """Whole pipeline: issuer signs -> wallet rerandomizes + ElGamal-encrypts ->
    proves -> verifier accepts."""
    rng = seeded_rng(50)
    issuer = Issuer.setup("atb-financial-ca", ATB_ADDR, rng=rng)

    applicant_kp = identity_keygen(rng=rng)
    cred = issuer.issue(ALICE_FIELDS, ALICE_ADDR,
                        applicant_pk=applicant_kp.pk, rng=rng)

    # Wallet receives, decrypts to confirm M, and rerandomizes sigma.
    M_recv = elgamal_decrypt(cred.delivery, applicant_kp.sk)
    assert eq(M_recv, mul(G1, cred.m))

    sigma_p, _ = rerandomize_for_registration(cred, rng=rng)

    # Wallet picks fresh randomness for its own on-chain ciphertext.
    r = rand_scalar(rng)
    E = elgamal_encrypt(mul(G1, cred.m), applicant_kp.pk, r)

    proof = registration_prove(
        sigma_p, cred.m, r, applicant_kp.pk, E, ALICE_ADDR, rng=rng,
    )
    assert registration_verify(
        sigma_p, E, applicant_kp.pk, issuer.pk_X, issuer.pk_Y, proof, ALICE_ADDR,
    )


def test_end_to_end_with_wrong_issuer_keypair_fails():
    """Switching the issuer's PS key (e.g. after rotation) breaks verification."""
    rng = seeded_rng(51)
    real     = Issuer.setup("atb-financial-ca", ATB_ADDR, rng=rng)
    rotated  = Issuer.setup("atb-financial-ca", ATB_ADDR, rng=rng)
    applicant_kp = identity_keygen(rng=rng)
    cred = real.issue(ALICE_FIELDS, ALICE_ADDR, applicant_pk=applicant_kp.pk, rng=rng)
    sigma_p, _ = rerandomize_for_registration(cred, rng=rng)
    r = rand_scalar(rng)
    E = elgamal_encrypt(mul(G1, cred.m), applicant_kp.pk, r)
    proof = registration_prove(
        sigma_p, cred.m, r, applicant_kp.pk, E, ALICE_ADDR, rng=rng,
    )
    # Verifier checks against the rotated key -> should fail.
    assert not registration_verify(
        sigma_p, E, applicant_kp.pk, rotated.pk_X, rotated.pk_Y, proof, ALICE_ADDR,
    )
