"""Reference-math tests for verifiable decryption -- the compelled-disclosure
primitive behind the EOA ApproveReceipt (alberta-buck-notes.org "Mutual Decryptability" / "The Non-Deniable-Receipt Invariant").

Completeness (the true decryption verifies) and soundness/non-frameability (a
false M, wrong key, tampered response, or replayed context all reject -- in
particular a prover holding the *real* sk still cannot name a *false* M).
"""

from __future__ import annotations

import random

from alberta_buck.wallet.bn254 import G1, ORDER, add, mul, rand_scalar
from alberta_buck.wallet.elgamal import elgamal_encrypt, identity_keygen
from alberta_buck.wallet.verifiable_decrypt import (
    VDProof,
    verifiable_decrypt_prove,
    verifiable_decrypt_verify,
)

ACCOUNT = 0x0b0b000000000000000000000000000000000b0b
CHAINID = 1


def _rng(seed: int):
    r = random.Random(seed)
    return lambda: r.getrandbits(256)


def _setup(seed: int):
    """A keypair, an identity point M, and E = Enc(M; pk)."""
    rng = _rng(seed)
    kp = identity_keygen(rng=rng)
    M = mul(G1, rand_scalar(rng))
    E = elgamal_encrypt(M, kp.pk, rand_scalar(rng))
    return kp, M, E, rng


def test_roundtrip():
    kp, M, E, rng = _setup(1)
    pf = verifiable_decrypt_prove(E, kp.sk, M, ACCOUNT, CHAINID, rng=rng)
    assert verifiable_decrypt_verify(E, kp.pk, M, pf, ACCOUNT, CHAINID)


def test_wrong_M_rejected():
    kp, M, E, rng = _setup(2)
    pf = verifiable_decrypt_prove(E, kp.sk, M, ACCOUNT, CHAINID, rng=rng)
    assert not verifiable_decrypt_verify(E, kp.pk, add(M, G1), pf, ACCOUNT, CHAINID)


def test_wrong_key_rejected():
    kp, M, E, rng = _setup(3)
    pf = verifiable_decrypt_prove(E, kp.sk, M, ACCOUNT, CHAINID, rng=rng)
    other = identity_keygen(rng=rng)
    assert not verifiable_decrypt_verify(E, other.pk, M, pf, ACCOUNT, CHAINID)


def test_tampered_response_rejected():
    kp, M, E, rng = _setup(4)
    pf = verifiable_decrypt_prove(E, kp.sk, M, ACCOUNT, CHAINID, rng=rng)
    bad = VDProof(e=pf.e, s=(pf.s + 1) % ORDER, T1=pf.T1, T2=pf.T2)
    assert not verifiable_decrypt_verify(E, kp.pk, M, bad, ACCOUNT, CHAINID)


def test_replay_other_account_or_chain_rejected():
    kp, M, E, rng = _setup(5)
    pf = verifiable_decrypt_prove(E, kp.sk, M, ACCOUNT, CHAINID, rng=rng)
    assert not verifiable_decrypt_verify(E, kp.pk, M, pf, ACCOUNT + 1, CHAINID)
    assert not verifiable_decrypt_verify(E, kp.pk, M, pf, ACCOUNT, CHAINID + 1)


def test_real_key_cannot_name_false_M():
    # Non-frameability: even with the genuine sk, a prover who claims a false M'
    # cannot produce an accepting proof -- Check 2 would force M' == C - sk*R.
    kp, M, E, rng = _setup(6)
    M_false = mul(G1, rand_scalar(rng))
    assert M_false != M
    pf = verifiable_decrypt_prove(E, kp.sk, M_false, ACCOUNT, CHAINID, rng=rng)
    assert not verifiable_decrypt_verify(E, kp.pk, M_false, pf, ACCOUNT, CHAINID)
