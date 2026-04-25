"""Chaum-Pedersen DLEQ for the A-spend identity binding (Phase 8 V2).

Reference: alberta-buck-proofs.org Theorem 8 #3, alberta-buck-ethereum.org
Phase 8 V2.

The A-spend circuit binds a private ``(rho, idHash, predicate)`` opening to
the leaf commitment, but does *not* tie the spender to the recipient public
key registered in IdentityRegistry: that constraint requires non-native BN254
G1 arithmetic in-circuit, which Phase 8 V1 deferred.

Phase 8 V2 ships the binding as an off-chain Chaum-Pedersen DLEQ proof
verified by Solidity using the EIP-196 BN254 precompiles.  The note's
ElGamal ciphertext ``E_n = (R_n, C_n)`` is exposed as four additional
public inputs to the SNARK, so the on-chain verifier and the SNARK agree
on which ciphertext is being spent.

The DLEQ statement is:

    Prove knowledge of sk_dep such that
        pk_dep             === sk_dep * G          (registered key)
        (C_reg - C_n)      === sk_dep * (R_reg - R_n)

The two equations together force ``C_reg - sk_dep*R_reg === C_n - sk_dep*R_n``
-- exactly the "single-sk_dep" CP-equality of Theorem 8 #3.  Identity
re-issuance produces a fresh (sk', pk') bound to the same identity point M
but a different public key; the second equation fails for the new key, so
A-notes are unspendable after sk_rec loss (the V2 cryptographic invariant).

Sigma protocol:
    Prover picks t in Fr, computes T1 = t*G, T2 = t*(R_reg - R_n).
    Challenge:  e = H(G, pk_dep, R_reg-R_n, C_reg-C_n, T1, T2,
                      R_n.x, R_n.y, C_n.x, C_n.y,
                      R_reg.x, R_reg.y, C_reg.x, C_reg.y,
                      pk_dep.x, pk_dep.y,
                      recipient, chainid)            mod ORDER
    Response:   s = t + e*sk_dep                     mod ORDER

Proof: ``(e, s, T1, T2)`` -- four field elements, ~36K gas to verify
on-chain (2 ecMul + 2 ecAdd + Fiat-Shamir keccak).
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
class SpendCPProof:
    """4-element CP-DLEQ proof for the A-spend identity binding."""
    e:  int
    s:  int
    T1: Tuple    # G1: t*G
    T2: Tuple    # G1: t*(R_reg - R_n)


def _spend_cp_transcript(
    E_n:       ElGamalCiphertext,
    E_reg:     ElGamalCiphertext,
    pk_dep,
    T1, T2,
    recipient: int,
    chainid:   int,
) -> int:
    """Fiat-Shamir transcript for the spend CP-DLEQ.

    Order MUST match IdentityRegistry._fsSpendCP byte-for-byte.  Each input
    is a uint256; G1 points contribute (X, Y) pairs.
    """
    Rnx, Rny = point_to_words(E_n.R)
    Cnx, Cny = point_to_words(E_n.C)
    Rrx, Rry = point_to_words(E_reg.R)
    Crx, Cry = point_to_words(E_reg.C)
    pkx, pky = point_to_words(pk_dep)
    T1x, T1y = point_to_words(T1)
    T2x, T2y = point_to_words(T2)
    return keccak_scalar(
        Rnx, Rny, Cnx, Cny,
        Rrx, Rry, Crx, Cry,
        pkx, pky,
        T1x, T1y, T2x, T2y,
        recipient, chainid,
    )


def spend_cp_prove(
    E_n:       ElGamalCiphertext,
    E_reg:     ElGamalCiphertext,
    pk_dep,
    sk_dep:    int,
    recipient: int,
    chainid:   int,
    rng=None,
) -> SpendCPProof:
    """Generate a CP-DLEQ proof for the A-spend identity binding.

    ``sk_dep`` is the recipient's secret key (== the spender's secret).
    The recipient's currently-registered ciphertext ``E_reg`` and the
    note's ciphertext ``E_n`` must both decrypt to the same identity
    point M under sk_dep -- if so, this proof verifies; otherwise it
    is unforgeable under the discrete-log assumption on G1.
    """
    H = add(E_reg.R, neg(E_n.R))     # H = R_reg - R_n
    t  = rand_scalar(rng)
    T1 = mul(G1, t)
    T2 = mul(H, t)
    e  = _spend_cp_transcript(E_n, E_reg, pk_dep, T1, T2, recipient, chainid)
    s  = (t + e * (sk_dep % ORDER)) % ORDER
    return SpendCPProof(e=e, s=s, T1=T1, T2=T2)


def spend_cp_verify(
    E_n:       ElGamalCiphertext,
    E_reg:     ElGamalCiphertext,
    pk_dep,
    proof:     SpendCPProof,
    recipient: int,
    chainid:   int,
) -> bool:
    """Verify a spend CP-DLEQ proof.  Returns True iff all three checks pass."""
    e, s = proof.e, proof.s
    H    = add(E_reg.R, neg(E_n.R))         # R_reg - R_n
    X2   = add(E_reg.C, neg(E_n.C))         # C_reg - C_n

    # Check 1: s*G == T1 + e*pk_dep
    lhs1 = mul(G1, s)
    rhs1 = add(proof.T1, mul(pk_dep, e))
    if not eq(lhs1, rhs1):
        return False

    # Check 2: s*(R_reg - R_n) == T2 + e*(C_reg - C_n)
    lhs2 = mul(H, s)
    rhs2 = add(proof.T2, mul(X2, e))
    if not eq(lhs2, rhs2):
        return False

    # Check 3: Fiat-Shamir
    e_check = _spend_cp_transcript(
        E_n, E_reg, pk_dep, proof.T1, proof.T2, recipient, chainid,
    )
    return e_check == e
