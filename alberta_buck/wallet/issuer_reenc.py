"""A2 issuer re-encryption binding -- recipient-blinded proof that a private
issuer's note ciphertext E_iss-for-rec re-encrypts the issuer's *registered*
Identity under the recipient's key, WITHOUT revealing the recipient.

Reference: alberta-buck-notes-decryptability.org ("The Fix: Issuer half, bound
at mint", Private issuer (A2), option 2 -- recipient-blinded on-chain CP).

Problem.  For an A2 (addressed, private-issuer) note the issuer attaches
``E_iss-for-rec = (R_i, C_i) = (r'*G, M_iss + r'*pk_rec)``, an ElGamal
re-encryption of the issuer's own registered Identity ``M_iss`` under the
recipient's key ``pk_rec``.  Mutual decryptability requires this to be *bound*
at mint: a verifier must confirm ``E_iss-for-rec`` really re-encrypts the
issuer's registered credential (not a random point), else a colluding issuer
pays a recipient while leaving no recoverable Identity.  But the verifier must
NOT learn ``pk_rec`` -- that would de-anonymise the recipient (A2's whole point).

This is the verifyApprove relation (sender = issuer, spender = recipient) with
``pk_rec`` hidden.  Hiding ``pk_rec`` turns the re-encryption term ``r'*pk_rec``
into a product of two secrets, which a naive sigma cannot prove.  The trick that
linearises it: publish ``U = r'*H`` and ``T = r'*pk_rec``, blind the key as
``Q = pk_rec + beta*H`` (H a second generator), and observe

    r'*Q - beta*U = r'*(pk_rec + beta*H) - beta*(r'*H) = r'*pk_rec = T

holds for *any* beta once ``U = r'*H`` is pinned -- so ``T = r'*pk_rec`` is
forced by a *linear* relation in the witnesses (r', beta).  No product gadget,
no pairing, no in-SNARK G1 arithmetic.

Statement.  Public: G, H, the issuer's registry record (pk_iss, R_reg, C_reg) =
(pk_iss, E_addr[issuer]); the leaf's E_iss = (R_i, C_i); and the issuer-published
blinding values Q, U, T.  The issuer proves knowledge of (r', beta, sk_iss):

    L1:  R_i               = r' * G                 (E_iss randomness)
    L2:  U                 = r' * H
    L3:  T                 = r' * Q - beta * U       (=> T = r'*pk_rec)
    L4:  pk_iss            = sk_iss * G              (registered key)
    L5:  C_reg + T - C_i   = sk_iss * R_reg          (=> C_i - T = M_iss,
                                                       the issuer's registered M)

L4+L5 prove ``C_i - T`` is the plaintext of the issuer's registered credential
(M_iss = C_reg - sk_iss*R_reg); L1-L3 prove ``T = r'*pk_rec`` for the key
committed in Q.  Composed: ``C_i = M_iss + r'*pk_rec`` with M_iss the registered
issuer Identity -- the A2 binding.

Recipient privacy.  The verifier sees Q (= pk_rec + beta*H, hiding for uniform
beta), U and T (= r'*H, r'*pk_rec -- uniform for fresh r', so they leak nothing
about pk_rec), and the ZK sigma transcript.  ``pk_rec`` itself never appears.

Recipient targeting (coupling, NOT in this module).  L1-L5 bind E_iss to the key
committed in Q, but do not by themselves prove that key is the *recipient's*.
That is enforced by tying Q to the same ``pk_rec`` used to form ``E_note`` (the
addressed note ciphertext the recipient scans + proves ownership of at spend via
verifySpendCP).  This module is the issuer-side binding; the E_note <-> Q tie is
a follow-on (see the doc's "Enforcing bearer => public issuer" / coupling).

The sigma is a standard multi-witness Okamoto proof (Fiat-Shamir), the same
shape as chaum_pedersen / spend_cp; an on-chain verifier would mirror the five
checks via EIP-196 BN254 precompiles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, mul, neg, eq, rand_scalar, point_to_words,
)
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.transcript import keccak_scalar, keccak_raw


# Second generator H -- a nothing-up-my-sleeve point.  Used only to hide pk_rec
# in Q (Pedersen-style); the construction's soundness does not rely on H having
# an unknown discrete log, so a hash-derived H is sufficient and reproducible.
H_SCALAR = int.from_bytes(
    keccak_raw(b"AlbertaBuck:IssuerReenc:H"), "big"
) % ORDER
H_POINT = mul(G1, H_SCALAR)


@dataclass(frozen=True)
class IssuerReencProof:
    """Okamoto sigma proof for the A2 issuer re-encryption binding.

    Carries the challenge ``e``, the three responses, and the five commitment
    points (so the Fiat-Shamir challenge can be recomputed).  The issuer also
    publishes the blinding values ``Q``, ``U``, ``T`` alongside the leaf; they
    are public inputs to :func:`issuer_reenc_verify`, returned here for
    convenience.
    """
    e:   int
    s_r: int
    s_b: int
    s_s: int
    A1:  Tuple  # k_r*G
    A2:  Tuple  # k_r*H
    A3:  Tuple  # k_r*Q - k_b*U
    A4:  Tuple  # k_s*G
    A5:  Tuple  # k_s*R_reg
    Q:   Tuple  # pk_rec + beta*H   (blinded recipient key)
    U:   Tuple  # r'*H
    T:   Tuple  # r'*pk_rec


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

    beta = rand_scalar(rng) if beta is None else (beta % ORDER)

    # Published blinding values.
    Q = add(pk_rec, mul(H_POINT, beta))      # pk_rec + beta*H
    U = mul(H_POINT, r_prime % ORDER)        # r'*H
    T = mul(pk_rec, r_prime % ORDER)         # r'*pk_rec

    # Y = C_reg + T - C_i = sk_iss*R_reg  (the L5 right-hand point).
    Y = add(C_reg, add(T, neg(C_i)))

    # Commitments.
    k_r = rand_scalar(rng)
    k_b = rand_scalar(rng)
    k_s = rand_scalar(rng)
    A1 = mul(G1, k_r)                                  # k_r*G
    A2 = mul(H_POINT, k_r)                             # k_r*H
    A3 = add(mul(Q, k_r), neg(mul(U, k_b)))           # k_r*Q - k_b*U
    A4 = mul(G1, k_s)                                  # k_s*G
    A5 = mul(R_reg, k_s)                               # k_s*R_reg

    e = _transcript(pk_iss, R_reg, C_reg, R_i, C_i, Q, U, T,
                    A1, A2, A3, A4, A5, issuer, chainid)

    s_r = (k_r + e * (r_prime % ORDER)) % ORDER
    s_b = (k_b + e * beta) % ORDER
    s_s = (k_s + e * (sk_iss % ORDER)) % ORDER

    return IssuerReencProof(e=e, s_r=s_r, s_b=s_b, s_s=s_s,
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
    e, s_r, s_b, s_s = proof.e, proof.s_r, proof.s_b, proof.s_s
    Q, U, T = proof.Q, proof.U, proof.T

    # L1: s_r*G == A1 + e*R_i
    if not eq(mul(G1, s_r), add(proof.A1, mul(R_i, e))):
        return False
    # L2: s_r*H == A2 + e*U
    if not eq(mul(H_POINT, s_r), add(proof.A2, mul(U, e))):
        return False
    # L3: s_r*Q - s_b*U == A3 + e*T   (=> T = r'*pk_rec)
    if not eq(add(mul(Q, s_r), neg(mul(U, s_b))), add(proof.A3, mul(T, e))):
        return False
    # L4: s_s*G == A4 + e*pk_iss
    if not eq(mul(G1, s_s), add(proof.A4, mul(pk_iss, e))):
        return False
    # L5: s_s*R_reg == A5 + e*(C_reg + T - C_i)   (=> C_i - T = M_iss)
    Y = add(C_reg, add(T, neg(C_i)))
    if not eq(mul(R_reg, s_s), add(proof.A5, mul(Y, e))):
        return False

    # Fiat-Shamir
    return proof.e == _transcript(pk_iss, R_reg, C_reg, R_i, C_i, Q, U, T,
                                  proof.A1, proof.A2, proof.A3, proof.A4, proof.A5,
                                  issuer, chainid)


__all__ = [
    "H_POINT", "H_SCALAR",
    "IssuerReencProof",
    "issuer_reenc_prove", "issuer_reenc_verify",
]
