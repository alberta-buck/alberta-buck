"""EC-ElGamal encryption over BN254 G1.

Encrypts a message *point* M (not a scalar): given recipient public key pk = sk*G,
ciphertext (R, C) = (r*G, M + r*pk).  Decryption: M = C - sk*R.

In the BUCK identity layer M = m*G where m = identity_scalar(identity_data),
so EC-ElGamal hides the identity point even from observers who later learn the
secret key (because they still need r to recover M from C without sk).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from alberta_buck.wallet.bn254 import G1, add, mul, neg, rand_scalar


@dataclass(frozen=True)
class IdentityKeyPair:
    sk: int
    pk: Tuple   # G1 point


@dataclass(frozen=True)
class ElGamalCiphertext:
    R: Tuple    # G1 point: r*G
    C: Tuple    # G1 point: M + r*pk


def identity_keygen(rng=None) -> IdentityKeyPair:
    sk = rand_scalar(rng)
    return IdentityKeyPair(sk=sk, pk=mul(G1, sk))


def elgamal_encrypt(M, pk, r: int) -> ElGamalCiphertext:
    """(R, C) = (r*G, M + r*pk)."""
    return ElGamalCiphertext(R=mul(G1, r), C=add(M, mul(pk, r)))


def elgamal_decrypt(ct: ElGamalCiphertext, sk: int):
    """M = C - sk*R."""
    return add(ct.C, neg(mul(ct.R, sk)))
