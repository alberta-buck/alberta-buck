"""A2 issuer re-encryption binding -- a recipient-blinded proof that a private
issuer's note ciphertext eIss carries the issuer's *registered* Identity, WITHOUT
revealing the recipient or the issuer.

Reference: alberta-buck-notes.org ("The Non-Deniable-Receipt Invariant", the A2
binding at mint) and doc/review/notes-receiving-key.org section 4.6 (the A2 key
split, and why the tie is committed).

Problem.  An A2 (addressed, private-issuer) note carries
``eIss = (R_i, C_i) = (r'*G, M_iss + r'*pk_rec)``, an ElGamal encryption of the
issuer's registered Identity ``M_iss`` to the recipient's mailbox key ``pk_rec``.
The mint must bind it: a verifier must confirm eIss carries the issuer's
registered credential (not a random point), else a colluding issuer pays a
recipient and leaves no recoverable Identity.  But the chain must learn NEITHER
``pk_rec`` (that links the recipient) NOR ``M_iss`` (that names the private
issuer -- A2's whole point).

This is the verifyApprove relation (sender = issuer, spender = recipient) with
both plaintexts hidden.  Hiding ``pk_rec`` makes ``r'*pk_rec`` a product of two
secrets.  The trick that linearises it: publish ``U = r'*H``, blind the key as
``Q = pk_rec + beta*H``, and observe

    r'*Q - beta*U = r'*(pk_rec + beta*H) - beta*(r'*H) = r'*pk_rec

for *any* beta once U pins r' -- so ``T = r'*pk_rec + gamma*H`` is forced by a
*linear* relation in (r', beta, gamma).  No product gadget, no pairing, no
in-SNARK G1 arithmetic.

H is :data:`alberta_buck.wallet.nums.H_PEDERSEN`, hashed to the curve, so no one
knows its logarithm to G.  The blinds depend on that.  Were ``H = h*G`` for a
public h, the minter could pay any difference of Identities in gamma, and the tie
below would bind nothing.

Why T is blinded (issuer privacy).  An unblinded ``r'*pk_rec`` would give any
observer ``M_iss = C_i - r'*pk_rec``, and at mint ``msg.sender`` is the issuer:
the chain would bind the issuer's address to its Identity.  gamma hides M_iss.
The recipient receives gamma wrapped in the delivery and discloses it only in a
receipt, where naming M_iss is the point.

Statement.  Public: G, H, the issuer's registry record (pk_iss, R_reg, C_reg);
the leaf's eIss = (R_i, C_i); and the issuer-published Q, U, T.  The issuer
proves knowledge of (r', beta, sk_iss, gamma):

    L1:  R_i               = r' * G
    L2:  U                 = r' * H
    L3:  T                 = r' * Q - beta * U + gamma * H
    L4:  pk_iss            = sk_iss * G
    L5:  C_reg + T - C_i   = sk_iss * R_reg + gamma * H

With ``pk_Q = Q - beta*H``, L3 gives ``T = r'*pk_Q + gamma*H``, and L5 then
gives ``C_i = (C_reg - sk_iss*R_reg) + r'*pk_Q = M_iss + r'*pk_Q``: eIss carries
the issuer's registered Identity to the key committed in Q, and gamma cancels so
M_iss is never exposed.

Privacy.  The chain sees Q (hides pk_rec for uniform beta), U (uniform), T
(uniform via gamma), and the ZK sigma transcript.

Which key.  L1-L5 bind eIss to pk_Q, not to the key the recipient holds.  An
ElGamal ciphertext does not bind its plaintext to one key: a minter can choose
pk_Q so that the same eIss also encrypts a registered sock puppet M_B to the real
mailbox, and the honest recipient's receipt would name M_B.  So the tie is
committed: idHash_a2 commits T, and the A2 deposit fold checks
``T = rm*G + gamma*H`` for the ``rm = r'*k`` it already enforces against the
recipient's k.  Together with L3 that is ``r'*(pk_Q - k*G) = (gamma' - gamma)*H``,
so re-aiming the key needs ``M_B - M_iss = delta*H`` for a delta the minter
knows: a discrete log.  A receipt checks ``M_named == C_i - T + gamma*H``.

The sigma is a standard multi-witness Okamoto proof (Fiat-Shamir), the same
shape as chaum_pedersen / spend_cp; the on-chain verifier (verifyIssuerReenc)
mirrors the five checks via EIP-196 BN254 precompiles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, mul, neg, eq, rand_scalar, point_to_words,
)
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.transcript import keccak_scalar
from alberta_buck.wallet.domains import FS_ISSUER_REENC, word as _word
from alberta_buck.wallet.nums import H_PEDERSEN as H


@dataclass(frozen=True)
class IssuerReencProof:
    """Okamoto sigma proof for the A2 issuer re-encryption binding.

    Carries the challenge ``e``, the four responses, and the five commitment
    points (so the Fiat-Shamir challenge can be recomputed).  The issuer also
    publishes the values ``Q``, ``U``, ``T`` alongside the leaf; they are public
    inputs to :func:`issuer_reenc_verify`, and ``T`` is committed in idHash_a2.
    """
    e:   int
    s_r: int
    s_b: int
    s_s: int
    s_g: int
    A1:  Tuple  # k_r*G
    A2:  Tuple  # k_r*H
    A3:  Tuple  # k_r*Q - k_b*U + k_g*H
    A4:  Tuple  # k_s*G
    A5:  Tuple  # k_s*R_reg + k_g*H
    Q:   Tuple  # pk_rec + beta*H            (blinded recipient key)
    U:   Tuple  # r'*H
    T:   Tuple  # r'*pk_rec + gamma*H        (blinds M_iss = C_i - r'*pk_rec)


def _transcript(pk_iss, R_reg, C_reg, R_i, C_i, Q, U, T,
                A1, A2, A3, A4, A5, issuer: int, chainid: int) -> int:
    """Fiat-Shamir challenge.  Binds the issuer registry record, the leaf
    ciphertext E_iss, the published blinding values, the commitments, and
    (issuer, chainid) so a proof cannot be replayed."""
    pts = [pk_iss, R_reg, C_reg, R_i, C_i, Q, U, T, A1, A2, A3, A4, A5]
    words = []
    for P in pts:
        x, y = point_to_words(P)
        words.append(x)
        words.append(y)
    words.append(issuer)
    words.append(chainid)
    words.append(_word(FS_ISSUER_REENC))
    return keccak_scalar(*words)


def issuer_reenc_prove(
    sk_iss:  int,
    r_prime: int,
    pk_rec,                      # G1 point: the recipient's registered key
    E_reg:   ElGamalCiphertext,  # issuer's registered credential (R_reg, C_reg)
    E_iss:   ElGamalCiphertext,  # the leaf's E_iss-for-rec (R_i, C_i)
    issuer:  int,
    chainid: int,
    beta:    Optional[int] = None,
    gamma:   Optional[int] = None,
    rng=None,
) -> IssuerReencProof:
    """Prove the A2 issuer re-encryption binding.

    The issuer must have formed ``E_iss = (r'*G, M_iss + r'*pk_rec)`` with
    ``M_iss = C_reg - sk_iss*R_reg`` their registered Identity.  Asserts that
    consistency before proving, so a misuse fails loudly rather than producing
    an unsound proof.
    """
    pk_iss = mul(G1, sk_iss % ORDER)
    R_reg, C_reg = E_reg.R, E_reg.C
    R_i, C_i = E_iss.R, E_iss.C

    # Sanity: the leaf must actually re-encrypt the issuer's registered M.
    M_iss = add(C_reg, neg(mul(R_reg, sk_iss % ORDER)))
    assert eq(R_i, mul(G1, r_prime % ORDER)), "E_iss.R != r'*G"
    assert eq(C_i, add(M_iss, mul(pk_rec, r_prime % ORDER))), \
        "E_iss.C != M_iss + r'*pk_rec"

    beta  = rand_scalar(rng) if beta  is None else (beta  % ORDER)
    gamma = rand_scalar(rng) if gamma is None else (gamma % ORDER)

    # Published values.  T is blinded by gamma*H so the chain cannot compute
    # M_iss = C_i - r'*pk_rec (which would name the private issuer).
    Q = add(pk_rec, mul(H, beta))                                # pk_rec + beta*H
    U = mul(H, r_prime % ORDER)                                  # r'*H
    T = add(mul(pk_rec, r_prime % ORDER), mul(H, gamma))         # r'*pk_rec + gamma*H

    # Commitments (witnesses r', beta, sk_iss, gamma).
    k_r = rand_scalar(rng)
    k_b = rand_scalar(rng)
    k_s = rand_scalar(rng)
    k_g = rand_scalar(rng)
    A1 = mul(G1, k_r)                                            # k_r*G
    A2 = mul(H, k_r)                                             # k_r*H
    A3 = add(add(mul(Q, k_r), neg(mul(U, k_b))), mul(H, k_g))    # k_r*Q - k_b*U + k_g*H
    A4 = mul(G1, k_s)                                            # k_s*G
    A5 = add(mul(R_reg, k_s), mul(H, k_g))                       # k_s*R_reg + k_g*H

    e = _transcript(pk_iss, R_reg, C_reg, R_i, C_i, Q, U, T,
                    A1, A2, A3, A4, A5, issuer, chainid)

    s_r = (k_r + e * (r_prime % ORDER)) % ORDER
    s_b = (k_b + e * beta) % ORDER
    s_s = (k_s + e * (sk_iss % ORDER)) % ORDER
    s_g = (k_g + e * gamma) % ORDER

    return IssuerReencProof(e=e, s_r=s_r, s_b=s_b, s_s=s_s, s_g=s_g,
                            A1=A1, A2=A2, A3=A3, A4=A4, A5=A5,
                            Q=Q, U=U, T=T)


def issuer_reenc_verify(
    pk_iss,
    E_reg:   ElGamalCiphertext,
    E_iss:   ElGamalCiphertext,
    proof:   IssuerReencProof,
    issuer:  int,
    chainid: int,
) -> bool:
    """Verify the A2 issuer re-encryption binding.  Returns True iff E_iss
    re-encrypts the issuer's registered Identity under the key committed in
    ``proof.Q``, without revealing it."""
    R_reg, C_reg = E_reg.R, E_reg.C
    R_i, C_i = E_iss.R, E_iss.C
    e, s_r, s_b, s_s, s_g = proof.e, proof.s_r, proof.s_b, proof.s_s, proof.s_g
    Q, U, T = proof.Q, proof.U, proof.T

    # L1: s_r*G == A1 + e*R_i
    if not eq(mul(G1, s_r), add(proof.A1, mul(R_i, e))):
        return False
    # L2: s_r*H == A2 + e*U
    if not eq(mul(H, s_r), add(proof.A2, mul(U, e))):
        return False
    # L3: s_r*Q - s_b*U + s_g*H == A3 + e*T   (=> T = r'*pk_Q + gamma*H)
    if not eq(add(add(mul(Q, s_r), neg(mul(U, s_b))), mul(H, s_g)),
              add(proof.A3, mul(T, e))):
        return False
    # L4: s_s*G == A4 + e*pk_iss
    if not eq(mul(G1, s_s), add(proof.A4, mul(pk_iss, e))):
        return False
    # L5: s_s*R_reg + s_g*H == A5 + e*(C_reg + T - C_i)
    Y = add(C_reg, add(T, neg(C_i)))
    if not eq(add(mul(R_reg, s_s), mul(H, s_g)), add(proof.A5, mul(Y, e))):
        return False

    # Fiat-Shamir
    return proof.e == _transcript(pk_iss, R_reg, C_reg, R_i, C_i, Q, U, T,
                                  proof.A1, proof.A2, proof.A3, proof.A4, proof.A5,
                                  issuer, chainid)


__all__ = [
    "IssuerReencProof",
    "issuer_reenc_prove", "issuer_reenc_verify",
]
