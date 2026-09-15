"""Unit tests for alberta_buck.wallet.

Validates the protocol round-trips and the documented counter-examples from
alberta-buck-identity-example.org against the Python implementation.
"""

from __future__ import annotations

import random

import pytest

from alberta_buck.wallet import (
    G1, ORDER, add, mul, neg, eq, is_inf,
    canonical_identity_data, identity_scalar,
    PSKeyPair, PSSignature, ps_keygen, ps_sign, ps_verify, ps_rerandomize,
    IdentityKeyPair, ElGamalCiphertext, identity_keygen, elgamal_encrypt, elgamal_decrypt,
    RegistrationProof, registration_prove, registration_verify,
    CPProof, chaum_pedersen_prove, chaum_pedersen_verify,
)
from alberta_buck.wallet.bn254 import rand_scalar


# A seeded RNG factory: the same seed produces the same vectors every run.
def seeded_rng(seed: int):
    rnd = random.Random(seed)
    def _r():
        return rnd.getrandbits(256)
    return _r


ALICE = {
    "given_name":    "Alice",
    "family_name":   "Johnson",
    "jurisdiction":  "Alberta, Canada",
    "id_type":       "Alberta Identity Card",
    "id_number":     "AIC-2026-4839201",
    "date_of_birth": "1992-03-15",
    "issuer_id":     "atb-financial-ca",
    "issued_at":     "2026-01-20T14:30:00Z",
    "epoch":         42,
}

BOB = {
    "given_name":    "Bob",
    "family_name":   "Smith",
    "jurisdiction":  "Alberta, Canada",
    "id_type":       "Corporate Registration",
    "id_number":     "AB-CORP-2026-00182",
    "date_of_birth": "1985-07-22",
    "issuer_id":     "atb-financial-ca",
    "issued_at":     "2026-02-01T09:00:00Z",
    "epoch":         42,
}


# --- Identity & PS round-trip ---------------------------------------------------

def test_identity_canonicalization_is_deterministic():
    a = canonical_identity_data(ALICE)
    b = canonical_identity_data({k: ALICE[k] for k in reversed(list(ALICE))})
    assert a == b
    assert identity_scalar(a) == identity_scalar(b)


def test_identity_scalar_in_range():
    m = identity_scalar(ALICE)
    assert 0 < m < ORDER


def test_ps_sign_and_verify():
    rng = seeded_rng(1)
    issuer = ps_keygen(rng=rng)
    m = identity_scalar(ALICE)
    sigma = ps_sign(issuer, m, rng=rng)
    assert ps_verify(issuer.pk_X, issuer.pk_Y, sigma, m)


def test_ps_verify_rejects_wrong_m():
    rng = seeded_rng(2)
    issuer = ps_keygen(rng=rng)
    sigma = ps_sign(issuer, identity_scalar(ALICE), rng=rng)
    assert not ps_verify(issuer.pk_X, issuer.pk_Y, sigma, identity_scalar(BOB))


def test_ps_rerandomize_preserves_validity():
    rng = seeded_rng(3)
    issuer = ps_keygen(rng=rng)
    m = identity_scalar(ALICE)
    sigma = ps_sign(issuer, m, rng=rng)
    sigma_p, _ = ps_rerandomize(sigma, rng=rng)
    assert ps_verify(issuer.pk_X, issuer.pk_Y, sigma_p, m)
    # And it's a different signature
    assert sigma.sigma_1 != sigma_p.sigma_1


# --- ElGamal round-trip ---------------------------------------------------------

def test_elgamal_encrypt_decrypt_roundtrip():
    rng = seeded_rng(4)
    kp = identity_keygen(rng=rng)
    M = mul(G1, identity_scalar(ALICE))
    r = rand_scalar(rng)
    ct = elgamal_encrypt(M, kp.pk, r)
    M_dec = elgamal_decrypt(ct, kp.sk)
    assert eq(M_dec, M)


def test_elgamal_wrong_key_fails():
    rng = seeded_rng(5)
    kp = identity_keygen(rng=rng)
    M = mul(G1, identity_scalar(ALICE))
    r = rand_scalar(rng)
    ct = elgamal_encrypt(M, kp.pk, r)
    bad_kp = identity_keygen(rng=rng)
    M_bad = elgamal_decrypt(ct, bad_kp.sk)
    assert not eq(M_bad, M)


# --- Registration NIZK ----------------------------------------------------------

def _full_registration_setup(seed: int, identity_fields=ALICE):
    rng = seeded_rng(seed)
    issuer = ps_keygen(rng=rng)
    m = identity_scalar(identity_fields)
    sigma = ps_sign(issuer, m, rng=rng)
    sigma_p, _ = ps_rerandomize(sigma, rng=rng)
    kp = identity_keygen(rng=rng)
    r = rand_scalar(rng)
    M = mul(G1, m)
    E = elgamal_encrypt(M, kp.pk, r)
    registrant = 0xa11ce00000000000000000000000000000a11ce
    proof = registration_prove(sigma_p, m, r, kp.pk, E, registrant, kp.sk, rng=rng)
    return issuer, sigma_p, kp, E, proof, registrant


def test_registration_proof_valid():
    issuer, sigma_p, kp, E, proof, registrant = _full_registration_setup(seed=10)
    assert registration_verify(sigma_p, E, kp.pk, issuer.pk_X, issuer.pk_Y, proof, registrant)


def test_registration_proof_rejects_wrong_registrant():
    issuer, sigma_p, kp, E, proof, registrant = _full_registration_setup(seed=11)
    other = registrant ^ 0xdeadbeef
    assert not registration_verify(sigma_p, E, kp.pk, issuer.pk_X, issuer.pk_Y, proof, other)


def test_registration_proof_rejects_wrong_chainid():
    issuer, sigma_p, kp, E, proof, registrant = _full_registration_setup(seed=14)
    assert not registration_verify(
        sigma_p, E, kp.pk, issuer.pk_X, issuer.pk_Y, proof, registrant, chainid=2,
    )


def test_registration_proof_rejects_infinity_pk():
    issuer, sigma_p, kp, E, proof, registrant = _full_registration_setup(seed=15)
    from alberta_buck.wallet.bn254 import Z1
    assert not registration_verify(
        sigma_p, E, Z1, issuer.pk_X, issuer.pk_Y, proof, registrant,
    )


def test_registration_proof_rejects_tampered_e():
    issuer, sigma_p, kp, E, proof, registrant = _full_registration_setup(seed=12)
    bad = RegistrationProof(
        e=(proof.e + 1) % ORDER, s_m=proof.s_m, s_r=proof.s_r, s_sk=proof.s_sk,
        A_ps=proof.A_ps, T_C=proof.T_C, T_R=proof.T_R, T_key=proof.T_key,
    )
    assert not registration_verify(sigma_p, E, kp.pk, issuer.pk_X, issuer.pk_Y, bad, registrant)


def test_registration_proof_rejects_mismatched_elgamal_m():
    """Counter-example from identity-example.org: encrypt m_fake but PS signed m_real."""
    rng = seeded_rng(13)
    issuer = ps_keygen(rng=rng)
    m_real = identity_scalar(ALICE)
    sigma = ps_sign(issuer, m_real, rng=rng)
    sigma_p, _ = ps_rerandomize(sigma, rng=rng)
    kp = identity_keygen(rng=rng)

    # Encrypt a *different* m
    m_fake = identity_scalar(BOB)
    r_fake = rand_scalar(rng)
    M_fake = mul(G1, m_fake)
    E_fake = elgamal_encrypt(M_fake, kp.pk, r_fake)
    registrant = 0xa11ce00000000000000000000000000000a11ce

    # Try to prove the real-m PS signature binds to the fake-m ciphertext.
    # The honest prover would reject; if the prover lies and uses (m_real, r_fake),
    # the ElGamal C check fails because C_fake encodes m_fake, not m_real.
    proof = registration_prove(sigma_p, m_real, r_fake, kp.pk, E_fake, registrant, kp.sk, rng=rng)
    assert not registration_verify(
        sigma_p, E_fake, kp.pk, issuer.pk_X, issuer.pk_Y, proof, registrant
    )


# --- Chaum-Pedersen re-encryption proof -----------------------------------------

def _approve_setup(seed: int):
    """Alice and Bob each register, then Alice re-encrypts her identity for Bob."""
    rng = seeded_rng(seed)

    issuer = ps_keygen(rng=rng)

    # Alice
    m_a = identity_scalar(ALICE)
    sigma_a = ps_sign(issuer, m_a, rng=rng)
    sigma_a_p, _ = ps_rerandomize(sigma_a, rng=rng)
    alice_kp = identity_keygen(rng=rng)
    r_a = rand_scalar(rng)
    M_a = mul(G1, m_a)
    E_a = elgamal_encrypt(M_a, alice_kp.pk, r_a)

    # Bob
    m_b = identity_scalar(BOB)
    sigma_b = ps_sign(issuer, m_b, rng=rng)
    sigma_b_p, _ = ps_rerandomize(sigma_b, rng=rng)
    bob_kp = identity_keygen(rng=rng)
    r_b_reg = rand_scalar(rng)
    M_b = mul(G1, m_b)
    E_b_reg = elgamal_encrypt(M_b, bob_kp.pk, r_b_reg)

    # Alice re-encrypts M_a for Bob
    r_prime = rand_scalar(rng)
    E_for_bob = elgamal_encrypt(M_a, bob_kp.pk, r_prime)

    sender_addr  = 0xa11ce00000000000000000000000000000a11ce
    spender_addr = 0xb0b0000000000000000000000000000000000b0b
    chainid      = 1

    proof = chaum_pedersen_prove(
        E_a, E_for_bob, alice_kp.pk, bob_kp.pk,
        alice_kp.sk, r_prime,
        sender_addr, spender_addr, chainid,
        rng=rng,
    )
    return {
        "alice_kp": alice_kp, "bob_kp": bob_kp,
        "E_a": E_a, "E_for_bob": E_for_bob,
        "M_a": M_a,
        "proof": proof,
        "sender": sender_addr, "spender": spender_addr, "chainid": chainid,
    }


def test_chaum_pedersen_proof_valid():
    s = _approve_setup(seed=20)
    assert chaum_pedersen_verify(
        s["E_a"], s["E_for_bob"], s["alice_kp"].pk, s["bob_kp"].pk,
        s["proof"], s["sender"], s["spender"], s["chainid"],
    )


def test_chaum_pedersen_bob_can_decrypt_to_M():
    s = _approve_setup(seed=21)
    M_dec = elgamal_decrypt(s["E_for_bob"], s["bob_kp"].sk)
    assert eq(M_dec, s["M_a"])


def test_chaum_pedersen_rejects_wrong_chainid():
    s = _approve_setup(seed=22)
    assert not chaum_pedersen_verify(
        s["E_a"], s["E_for_bob"], s["alice_kp"].pk, s["bob_kp"].pk,
        s["proof"], s["sender"], s["spender"], s["chainid"] + 1,
    )


def test_chaum_pedersen_rejects_wrong_spender():
    s = _approve_setup(seed=23)
    assert not chaum_pedersen_verify(
        s["E_a"], s["E_for_bob"], s["alice_kp"].pk, s["bob_kp"].pk,
        s["proof"], s["sender"], s["spender"] ^ 1, s["chainid"],
    )


def test_chaum_pedersen_rejects_wrong_M():
    """Counter-example: Alice tries to pass off a different identity point."""
    rng = seeded_rng(24)
    issuer = ps_keygen(rng=rng)
    m = identity_scalar(ALICE)
    sigma_p, _ = ps_rerandomize(ps_sign(issuer, m, rng=rng), rng=rng)
    alice_kp = identity_keygen(rng=rng)
    bob_kp   = identity_keygen(rng=rng)

    r_a = rand_scalar(rng)
    E_a = elgamal_encrypt(mul(G1, m), alice_kp.pk, r_a)

    # Encrypt a *different* M' for Bob
    m_other = identity_scalar(BOB)
    M_other = mul(G1, m_other)
    r_bad = rand_scalar(rng)
    E_bad = elgamal_encrypt(M_other, bob_kp.pk, r_bad)

    proof = chaum_pedersen_prove(
        E_a, E_bad, alice_kp.pk, bob_kp.pk,
        alice_kp.sk, r_bad,
        0xa11ce, 0xb0b, 1, rng=rng,
    )
    # Even with a "valid" prover transcript over the bad ciphertext, Check 3
    # (difference relation) fails because C_bad - C_a does not reflect the
    # same underlying M.
    assert not chaum_pedersen_verify(
        E_a, E_bad, alice_kp.pk, bob_kp.pk, proof,
        0xa11ce, 0xb0b, 1,
    )
