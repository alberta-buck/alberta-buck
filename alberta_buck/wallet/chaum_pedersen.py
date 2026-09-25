"""Chaum-Pedersen NIZK of equal-plaintext re-encryption.

Alice, who registered (E_alice = (R_a, C_a), pk_alice) holding identity point M,
gives Bob a fresh encryption E_bob = (R_b, C_b) of the same M under pk_bob, and
proves they decrypt to the same M -- without revealing M, sk_alice or the
re-encryption randomness r'.

Witnesses:  sk (secret key for pk_alice), r' (randomness for E_bob).
Public:     E_alice, E_bob, pk_alice, pk_bob, sender, spender, chainid,
            and registry.

The statement is the three-relation protocol of alberta-buck-proofs.org Part II
(S1/S2/S3) and doc/review/identity-findings.md Sec. 3:

  pk_a       = sk * G
  R_b        = r' * G
  C_a - C_b  = sk * R_a - r' * pk_b

Wire encoding (ABI-stable): the existing 6-field CPProof (e, s1, s2, T1, T2, T3)
is KEPT.  After the three-relation repair all three commitments are reconstructible
from (e, u, v), so compact (e, s1, s2) would be sound; we still send the three T's
so the on-chain ABI does not change.  The earlier comment that compact (e, s1, s2)
is unsound applied to the TWO-relation transcript (T1 and T2 hashed separately,
only T1-T2 constrained).  Reinterpretation:

  s1 = u = a + e*sk
  s2 = v = b + e*r'
  T1 = T_key  = a*G
  T3 = T_R    = b*G
  T2 = T_diff = a*R_a - b*pk_b

Challenge: e = H(E_alice, E_bob, pk_alice, pk_bob, T1, T2, T3,
                 sender, spender, chainid, registry)  mod ORDER

Verifier:

  s1*G              == T1 + e*pk_a
  s2*G              == T3 + e*R_b
  s1*R_a - s2*pk_b  == T2 + e*(C_a - C_b)
  Fiat-Shamir e was honestly derived

Account keys and both R values must be non-infinity; scalars must be canonical
(0 <= s < ORDER).  C may legitimately be the point at infinity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from py_ecc.bn128 import is_on_curve, b as curve_b

from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, mul, neg, eq, is_inf, rand_scalar, point_to_words,
)
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.transcript import keccak_scalar
from alberta_buck.wallet.domains import FS_APPROVE, word as _word


@dataclass(frozen=True)
class CPProof:
    e:  int
    s1: int
    s2: int
    T1: Tuple    # G1  T_key  = a*G
    T2: Tuple    # G1  T_diff = a*R_a - b*pk_b
    T3: Tuple    # G1  T_R    = b*G


def _canonical(s: int) -> bool:
    return isinstance(s, int) and 0 <= s < ORDER


def _on_curve(P) -> bool:
    if P is None or is_inf(P):
        return False
    return is_on_curve(P, curve_b)


def _on_curve_or_inf(P) -> bool:
    if P is None or is_inf(P):
        return True
    return is_on_curve(P, curve_b)


def _cp_transcript(
    E_alice: ElGamalCiphertext,
    E_bob:   ElGamalCiphertext,
    pk_alice,
    pk_bob,
    T1, T2, T3,
    sender:  int,
    spender: int,
    chainid: int,
    registry: int,
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
        sender, spender, chainid, registry,
        _word(FS_APPROVE),
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
    *,
    registry:  int = 0,
) -> CPProof:
    a = rand_scalar(rng)
    b = rand_scalar(rng)
    T1 = mul(G1, a)                                      # T_key
    T3 = mul(G1, b)                                      # T_R
    T2 = add(mul(E_alice.R, a), neg(mul(pk_bob, b)))     # T_diff
    e  = _cp_transcript(
        E_alice, E_bob, pk_alice, pk_bob, T1, T2, T3,
        sender, spender, chainid, registry,
    )
    s1 = (a + e * (sk_alice % ORDER)) % ORDER
    s2 = (b + e * (r_prime  % ORDER)) % ORDER
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
    registry:  int = 0,
) -> bool:
    e, s1, s2 = proof.e, proof.s1, proof.s2
    if not all(_canonical(s) for s in (e, s1, s2)):
        return False
    if not all(_on_curve(p) for p in (E_alice.R, E_bob.R, pk_alice, pk_bob)):
        return False
    if not all(_on_curve_or_inf(p) for p in (E_alice.C, E_bob.C, proof.T1, proof.T2, proof.T3)):
        return False

    # Check 1: s1*G == T1 + e*pk_a  (key ownership)
    if not eq(mul(G1, s1), add(proof.T1, mul(pk_alice, e))):
        return False

    # Check 2: s2*G == T3 + e*R_b
    if not eq(mul(G1, s2), add(proof.T3, mul(E_bob.R, e))):
        return False

    # Check 3: s1*R_a - s2*pk_b == T2 + e*(C_a - C_b)
    lhs = add(mul(E_alice.R, s1), neg(mul(pk_bob, s2)))
    rhs = add(proof.T2, mul(add(E_alice.C, neg(E_bob.C)), e))
    if not eq(lhs, rhs):
        return False

    e_check = _cp_transcript(
        E_alice, E_bob, pk_alice, pk_bob,
        proof.T1, proof.T2, proof.T3,
        sender, spender, chainid, registry,
    )
    return e_check == e
