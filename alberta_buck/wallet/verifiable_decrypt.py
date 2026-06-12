"""Verifiable decryption -- prove an ElGamal ciphertext decrypts to a *revealed*
identity point M under a registered key, without exposing the secret key.

Reference: alberta-buck-notes-decryptability.org (the "no deniability under
collusion" clause of the non-deniable-receipt invariant).

For a direct Identity-bound EOA transfer the approve handshake
(:mod:`alberta_buck.wallet.chaum_pedersen`) proves the ciphertext handed to the
recipient re-encrypts the sender's *registered* Identity -- but nobody can
*name* that Identity without the recipient's secret key, and merely decrypting
it yields an unprovable claim.  A compelled recipient must be able to prove to a
third party *which* M they decrypted.  This primitive is that proof: it makes
the recovered Identity publicly checkable while keeping sk private.

Statement: given E = (R, C) encrypted under pk = sk*G and a claimed plaintext
point M, prove knowledge of sk such that ::

    pk     === sk * G
    C - M  === sk * R          (equivalently  M === C - sk*R, the decryption)

revealing M but not sk.  A Chaum-Pedersen DLEQ on bases (G, R) -- the same shape
as :class:`alberta_buck.wallet.spend_cp.SpendCPProof`, specialized to the points
(R, C - M).

Sigma protocol::

    Prover picks t in Fr; T1 = t*G, T2 = t*R.
    Challenge:  e = H(R, C, pk, M, T1, T2, account, chainid)   mod ORDER
    Response:   s = t + e*sk                                   mod ORDER

Proof: ``(e, s, T1, T2)``.  Verify: s*G == T1 + e*pk and s*R == T2 + e*(C - M),
then recompute e.  ``account`` (the address whose key decrypts) and ``chainid``
are folded into the transcript so a proof is bound to its context and cannot be
replayed.

No on-chain verifier ships today (the receipt is checked off-chain, like the
Notes RcptVerify); a future on-chain version must mirror this transcript order.
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
class VDProof:
    """4-element Chaum-Pedersen DLEQ proof of correct ElGamal decryption."""
    e:  int
    s:  int
    T1: Tuple    # G1: t*G
    T2: Tuple    # G1: t*R


def _vd_transcript(E: ElGamalCiphertext, pk, M, T1, T2, account: int, chainid: int) -> int:
    Rx, Ry = point_to_words(E.R)
    Cx, Cy = point_to_words(E.C)
    pkx, pky = point_to_words(pk)
    Mx, My = point_to_words(M)
    T1x, T1y = point_to_words(T1)
    T2x, T2y = point_to_words(T2)
    return keccak_scalar(
        Rx, Ry, Cx, Cy,
        pkx, pky,
        Mx, My,
        T1x, T1y, T2x, T2y,
        account, chainid,
    )


def verifiable_decrypt_prove(
    E:       ElGamalCiphertext,
    sk:      int,
    M,
    account: int,
    chainid: int,
    rng=None,
) -> VDProof:
    """Prove that ``E`` decrypts to ``M`` under ``pk = sk*G``, revealing M.

    The honest prover supplies ``M = C - sk*R`` (the true decryption); a false
    ``M`` cannot satisfy the second verification equation under any key.
    """
    pk = mul(G1, sk % ORDER)
    t  = rand_scalar(rng)
    T1 = mul(G1, t)
    T2 = mul(E.R, t)
    e  = _vd_transcript(E, pk, M, T1, T2, account, chainid)
    s  = (t + e * (sk % ORDER)) % ORDER
    return VDProof(e=e, s=s, T1=T1, T2=T2)


def verifiable_decrypt_verify(
    E:       ElGamalCiphertext,
    pk,
    M,
    proof:   VDProof,
    account: int,
    chainid: int,
) -> bool:
    """Verify a verifiable-decryption proof.  Returns True iff ``M`` is exactly
    the decryption of ``E`` under the key ``pk``."""
    e, s = proof.e, proof.s
    X2 = add(E.C, neg(M))   # C - M

    # Check 1: s*G == T1 + e*pk            (sk is the discrete log of pk)
    if not eq(mul(G1, s), add(proof.T1, mul(pk, e))):
        return False

    # Check 2: s*R == T2 + e*(C - M)       (same sk gives M = C - sk*R)
    if not eq(mul(E.R, s), add(proof.T2, mul(X2, e))):
        return False

    # Check 3: Fiat-Shamir
    return proof.e == _vd_transcript(E, pk, M, proof.T1, proof.T2, account, chainid)
