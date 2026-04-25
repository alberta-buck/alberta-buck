"""Tests for the A-spend Chaum-Pedersen DLEQ (Phase 8 V2 identity binding).

Mirrors the threat model the on-chain Solidity verifier defends against:
honest spends pass; identity re-issuance, transcript tampering, and
cross-context replay are all rejected.
"""

from __future__ import annotations

import os
import random as _rand

import pytest

from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, mul, neg, eq, rand_scalar,
)
from alberta_buck.wallet.elgamal import (
    elgamal_encrypt, identity_keygen,
)
from alberta_buck.wallet.spend_cp import (
    SpendCPProof, spend_cp_prove, spend_cp_verify,
)


# Deterministic RNG so test failures reproduce.
def _seeded_rng(seed: int):
    rnd = _rand.Random(seed)
    return lambda: rnd.getrandbits(256)


def _setup(seed: int = 1):
    """Return a self-consistent A-spend identity binding scenario.

    Steps:
      1. Recipient generates an identity key (sk_dep, pk_dep).
      2. Identity is registered as E_reg = (R_reg, C_reg) under pk_dep
         encrypting an identity point M.
      3. Issuer mints an A2-flavor note for the same recipient: encrypts
         the same M under the same pk_dep with fresh randomness, yielding
         E_n = (R_n, C_n).

    By construction, sk_dep decrypts both ciphertexts to the same M, so
    the CP-DLEQ should verify.
    """
    rng = _seeded_rng(seed)
    keys     = identity_keygen(rng)
    # Identity point M = m*G for some m chosen by the issuer.
    m        = rand_scalar(rng)
    M        = mul(G1, m)
    r_reg    = rand_scalar(rng)
    r_note   = rand_scalar(rng)
    E_reg    = elgamal_encrypt(M, keys.pk, r_reg)
    E_n      = elgamal_encrypt(M, keys.pk, r_note)
    return keys, M, E_reg, E_n


def test_honest_spend_verifies():
    keys, _M, E_reg, E_n = _setup()
    pi = spend_cp_prove(E_n, E_reg, keys.pk, keys.sk,
                        recipient=0xCAFE, chainid=1, rng=_seeded_rng(2))
    assert spend_cp_verify(E_n, E_reg, keys.pk, pi,
                           recipient=0xCAFE, chainid=1)


def test_wrong_secret_key_rejected():
    """A holder of a different sk_dep cannot forge a CP-DLEQ for someone
    else's note -- this is the "identity re-issuance" attack: even though
    M is the same, the new (sk', pk') breaks DLEQ on pk_dep."""
    keys, _M, E_reg, E_n = _setup()
    other = identity_keygen(_seeded_rng(99))
    pi = spend_cp_prove(E_n, E_reg, keys.pk, other.sk,
                        recipient=0xCAFE, chainid=1, rng=_seeded_rng(2))
    assert not spend_cp_verify(E_n, E_reg, keys.pk, pi,
                               recipient=0xCAFE, chainid=1)


def test_re_registered_recipient_cannot_spend_old_note():
    """Recipient lost sk_rec, re-registered with (sk', pk') bound to the
    same identity point M.  The old note's E_n still encrypts under the
    OLD pk_rec, so even the new (legitimate) holder cannot satisfy
    pk_dep === sk_dep*G with their new key."""
    rng = _seeded_rng(7)
    M     = mul(G1, rand_scalar(rng))
    keys_old = identity_keygen(rng)
    keys_new = identity_keygen(rng)

    # Old: issuer minted a note encrypted under pk_old.
    E_n   = elgamal_encrypt(M, keys_old.pk, rand_scalar(rng))
    # New: identity re-registered under pk_new (same M).
    E_reg = elgamal_encrypt(M, keys_new.pk, rand_scalar(rng))

    # The new keyholder tries to spend the old note.  CP-DLEQ asks for a
    # single sk that satisfies BOTH pk_new === sk*G AND decrypts E_n,
    # which is impossible (sk_new doesn't decrypt E_n; sk_old isn't bound
    # to pk_new).
    pi = spend_cp_prove(E_n, E_reg, keys_new.pk, keys_new.sk,
                        recipient=0xBEEF, chainid=1, rng=_seeded_rng(3))
    assert not spend_cp_verify(E_n, E_reg, keys_new.pk, pi,
                               recipient=0xBEEF, chainid=1)

    # Symmetrically the old key fails the first check (sk_old != log_G(pk_new)).
    pi2 = spend_cp_prove(E_n, E_reg, keys_new.pk, keys_old.sk,
                         recipient=0xBEEF, chainid=1, rng=_seeded_rng(4))
    assert not spend_cp_verify(E_n, E_reg, keys_new.pk, pi2,
                               recipient=0xBEEF, chainid=1)


def test_tampered_note_ciphertext_rejected():
    keys, _M, E_reg, E_n = _setup()
    pi = spend_cp_prove(E_n, E_reg, keys.pk, keys.sk,
                        recipient=0xCAFE, chainid=1, rng=_seeded_rng(2))

    # Tamper with C_n: replace with an arbitrary point.
    tampered_C = add(E_n.C, mul(G1, 1))
    E_n_bad = type(E_n)(R=E_n.R, C=tampered_C)
    assert not spend_cp_verify(E_n_bad, E_reg, keys.pk, pi,
                               recipient=0xCAFE, chainid=1)


def test_tampered_proof_fields_rejected():
    keys, _M, E_reg, E_n = _setup()
    pi = spend_cp_prove(E_n, E_reg, keys.pk, keys.sk,
                        recipient=0xCAFE, chainid=1, rng=_seeded_rng(2))

    # Bump e by 1 -- breaks both algebraic checks AND the FS recompute.
    bad_e = SpendCPProof(e=(pi.e + 1) % ORDER, s=pi.s, T1=pi.T1, T2=pi.T2)
    assert not spend_cp_verify(E_n, E_reg, keys.pk, bad_e,
                               recipient=0xCAFE, chainid=1)

    # Bump s by 1 -- algebraic checks fail.
    bad_s = SpendCPProof(e=pi.e, s=(pi.s + 1) % ORDER, T1=pi.T1, T2=pi.T2)
    assert not spend_cp_verify(E_n, E_reg, keys.pk, bad_s,
                               recipient=0xCAFE, chainid=1)

    # Replace T1 with a random point.
    bad_T1 = SpendCPProof(e=pi.e, s=pi.s,
                          T1=mul(G1, 12345), T2=pi.T2)
    assert not spend_cp_verify(E_n, E_reg, keys.pk, bad_T1,
                               recipient=0xCAFE, chainid=1)

    # Replace T2 with a random point.
    bad_T2 = SpendCPProof(e=pi.e, s=pi.s,
                          T1=pi.T1, T2=mul(G1, 67890))
    assert not spend_cp_verify(E_n, E_reg, keys.pk, bad_T2,
                               recipient=0xCAFE, chainid=1)


def test_replay_across_chains_rejected():
    """Fiat-Shamir binds chainid; a proof on chain 1 must not verify on chain 2."""
    keys, _M, E_reg, E_n = _setup()
    pi = spend_cp_prove(E_n, E_reg, keys.pk, keys.sk,
                        recipient=0xCAFE, chainid=1, rng=_seeded_rng(2))
    assert spend_cp_verify(E_n, E_reg, keys.pk, pi,
                           recipient=0xCAFE, chainid=1)
    assert not spend_cp_verify(E_n, E_reg, keys.pk, pi,
                               recipient=0xCAFE, chainid=2)


def test_replay_across_recipients_rejected():
    """Fiat-Shamir binds recipient; a spend to Alice must not verify as a spend to Bob."""
    keys, _M, E_reg, E_n = _setup()
    pi = spend_cp_prove(E_n, E_reg, keys.pk, keys.sk,
                        recipient=0xAAAA, chainid=1, rng=_seeded_rng(2))
    assert spend_cp_verify(E_n, E_reg, keys.pk, pi,
                           recipient=0xAAAA, chainid=1)
    assert not spend_cp_verify(E_n, E_reg, keys.pk, pi,
                               recipient=0xBBBB, chainid=1)


def test_zero_knowledge_independence_of_witness():
    """Two honest provers with different randomness produce different proofs
    that both verify -- showing the proof reveals nothing about t."""
    keys, _M, E_reg, E_n = _setup()
    pi1 = spend_cp_prove(E_n, E_reg, keys.pk, keys.sk,
                         recipient=0xCAFE, chainid=1, rng=_seeded_rng(11))
    pi2 = spend_cp_prove(E_n, E_reg, keys.pk, keys.sk,
                         recipient=0xCAFE, chainid=1, rng=_seeded_rng(22))
    assert pi1 != pi2
    assert spend_cp_verify(E_n, E_reg, keys.pk, pi1,
                           recipient=0xCAFE, chainid=1)
    assert spend_cp_verify(E_n, E_reg, keys.pk, pi2,
                           recipient=0xCAFE, chainid=1)
