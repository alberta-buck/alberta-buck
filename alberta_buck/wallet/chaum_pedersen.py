"""Chaum-Pedersen NIZK of equal-plaintext re-encryption.

Alice, who registered (E_alice = (R_a, C_a), pk_alice) holding identity point M,
gives Bob a fresh encryption E_bob = (R_b, C_b) of the same M under pk_bob, and
proves they decrypt to the same M -- without revealing M, sk_alice or the
re-encryption randomness r'.

Witnesses:  sk_alice (secret key for E_alice), r' (randomness for E_bob).
Public:     E_alice, E_bob, pk_alice, pk_bob, sender, spender, chainid.

Commitments:  T1 = k1*R_a,  T2 = k2*pk_bob,  T3 = k2*G  (k1, k2 random)
Challenge:    e  = H(E_alice, E_bob, pk_alice, pk_bob, T1, T2, T3,
                     sender, spender, chainid)            mod ORDER
Responses:    s1 = k1 + e*sk_alice,  s2 = k2 + e*r'      mod ORDER

On-chain proof carries (e, s1, s2, T1, T2, T3) -- the verifier needs T1, T2, T3
to reconstruct the Fiat-Shamir hash.  (Naive (e, s1, s2) is *not* sound: only
T1-T2 and T3 can be recovered from the verification equations, so a transcript
binding T1 and T2 separately requires both to be in calldata.)

Verifier checks (all in G1, ~36K gas via ecAdd/ecMul + the extra calldata):

  Check 1: s2*G              == T3 + e*R_b               -- r' consistent with R_b
  Check 2: s1*R_a - s2*pk_b  == (T1 - T2) + e*(C_a - C_b) -- same M in both
  Check 3: Fiat-Shamir e was honestly derived
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, mul, neg, eq, rand_scalar, point_to_words,
)
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.transcript import keccak_scalar


@dataclass(frozen=True)
class CPProof:
    e:  int
    s1: int
    s2: int
    T1: Tuple    # G1
    T2: Tuple    # G1
    T3: Tuple    # G1


def _cp_transcript(
    E_alice: ElGamalCiphertext,
    E_bob:   ElGamalCiphertext,
    pk_alice,
    pk_bob,
    T1, T2, T3,
    sender:  int,
    spender: int,
    chainid: int,
) -> int:
    Rax, Ray = point_to_words(E_alice.R)
    Cax, Cay = point_to_words(E_alice.C)
    Rbx, Rby = point_to_words(E_bob.R)
    Cbx, Cby = point_to_words(E_bob.C)
    pax, pay = point_to_words(pk_alice)
    pbx, pby = point_to_words(pk_bob)
    T1x, T1y = point_to_words(T1)
    T2x, T2y = point_to_words(T2)
    T3x, T3y = point_to_words(T3)
    return keccak_scalar(
        Rax, Ray, Cax, Cay,
        Rbx, Rby, Cbx, Cby,
        pax, pay, pbx, pby,
        T1x, T1y, T2x, T2y, T3x, T3y,
        sender, spender, chainid,
    )


def chaum_pedersen_prove(
    E_alice:   ElGamalCiphertext,
    E_bob:     ElGamalCiphertext,
    pk_alice,
    pk_bob,
    sk_alice:  int,
    r_prime:   int,
    sender:    int,
    spender:   int,
    chainid:   int,
    rng=None,
) -> CPProof:
    k1 = rand_scalar(rng)
    k2 = rand_scalar(rng)
    T1 = mul(E_alice.R, k1)
    T2 = mul(pk_bob, k2)
    T3 = mul(G1, k2)
    e  = _cp_transcript(
        E_alice, E_bob, pk_alice, pk_bob, T1, T2, T3, sender, spender, chainid,
    )
    s1 = (k1 + e * (sk_alice % ORDER)) % ORDER
    s2 = (k2 + e * (r_prime  % ORDER)) % ORDER
    return CPProof(e=e, s1=s1, s2=s2, T1=T1, T2=T2, T3=T3)


def chaum_pedersen_verify(
    E_alice:   ElGamalCiphertext,
    E_bob:     ElGamalCiphertext,
    pk_alice,
    pk_bob,
    proof:     CPProof,
    sender:    int,
    spender:   int,
    chainid:   int,
) -> bool:
    e, s1, s2 = proof.e, proof.s1, proof.s2

    # Check 1: s2*G == T3 + e*R_b
    lhs1 = mul(G1, s2)
    rhs1 = add(proof.T3, mul(E_bob.R, e))
    if not eq(lhs1, rhs1):
        return False

    # Check 2: s1*R_a - s2*pk_b == (T1 - T2) + e*(C_a - C_b)
    lhs2 = add(mul(E_alice.R, s1), neg(mul(pk_bob, s2)))
    rhs2 = add(add(proof.T1, neg(proof.T2)), mul(add(E_alice.C, neg(E_bob.C)), e))
    if not eq(lhs2, rhs2):
        return False

    # Check 3: Fiat-Shamir
    e_check = _cp_transcript(
        E_alice, E_bob, pk_alice, pk_bob,
        proof.T1, proof.T2, proof.T3,
        sender, spender, chainid,
    )
    return e_check == e
