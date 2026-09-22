"""A2 issuer re-encryption binding -- recipient-blinded proof that a private
issuer's note ciphertext E_iss-for-rec re-encrypts the issuer's *registered*
Identity under the recipient's key, WITHOUT revealing the recipient.

Reference: alberta-buck-notes.org ("The Non-Deniable-Receipt Invariant", A2 issuer re-encryption binding at mint) and alberta-buck-notes-flow.org (A2 flows, note <-> eEnc tie). The fix for the issuer half at mint is implemented via the recipient-blinded re-encryption proof.

Problem.  For an A2 (addressed, private-issuer) note the issuer attaches
``E_iss-for-rec = (R_i, C_i) = (r'*G, M_iss + r'*pk_rec)``, an ElGamal
re-encryption of the issuer's own registered Identity ``M_iss`` under the
recipient's key ``pk_rec``.  Mutual decryptability requires this to be *bound*
at mint: a verifier must confirm ``E_iss-for-rec`` really re-encrypts the
issuer's registered credential (not a random point), else a colluding issuer
pays a recipient while leaving no recoverable Identity.  But the on-chain
verifier must learn NEITHER ``pk_rec`` (that de-anonymises the recipient) NOR
``M_iss`` (that de-anonymises the private issuer -- A2's whole point).

This is the verifyApprove relation (sender = issuer, spender = recipient) with
both plaintexts hidden.  Hiding ``pk_rec`` turns the re-encryption term
``r'*pk_rec`` into a product of two secrets.  The trick that linearises it:
publish ``U = r'*H`` and a blinded ``T_hat = r'*pk_rec + gamma*G``, blind the
key as ``Q = pk_rec + beta*H`` (H a second generator), and observe

    r'*Q - beta*U = r'*(pk_rec + beta*H) - beta*(r'*H) = r'*pk_rec

holds for *any* beta once ``U = r'*H`` is pinned -- so ``T_hat = r'*pk_rec +
gamma*G`` is forced by a *linear* relation in (r', beta, gamma).  No product
gadget, no pairing, no in-SNARK G1 arithmetic.

Why T is blinded (issuer privacy).  An unblinded ``T = r'*pk_rec`` would let any
observer recover ``M_iss = C_i - T`` (since ``C_i = M_iss + r'*pk_rec``), and at
mint ``msg.sender`` is the issuer -- so it would publicly bind the issuer's
address to its Identity, defeating A2.  The ``gamma*G`` blind hides M_iss from
the chain; the recipient discloses ``gamma`` (equivalently T) only in a compelled
receipt, where naming M_iss is the point.

Statement.  Public: G, H, the issuer's registry record (pk_iss, R_reg, C_reg) =
(pk_iss, E_addr[issuer]); the leaf's E_iss = (R_i, C_i); and the issuer-published
values Q, U, T_hat.  The issuer proves knowledge of (r', beta, sk_iss, gamma):

    L1:  R_i                   = r' * G                   (E_iss randomness)
    L2:  U                     = r' * H
    L3:  T_hat                 = r' * Q - beta * U + gamma * G   (=> r'*pk_rec)
    L4:  pk_iss                = sk_iss * G               (registered key)
    L5:  C_reg + T_hat - C_i   = sk_iss * R_reg + gamma * G

L3 forces ``T_hat = r'*pk_rec + gamma*G`` (T = T_hat - gamma*G = r'*pk_rec for
the key committed in Q).  L5 then gives ``C_i = (C_reg - sk_iss*R_reg) + T =
M_iss + r'*pk_rec`` with M_iss the issuer's registered Identity -- the A2
binding -- while gamma cancels so M_iss is never exposed.

Privacy.  The verifier sees Q (hiding pk_rec for uniform beta), U (= r'*H,
uniform), T_hat (uniform via gamma -- hides both pk_rec and M_iss), and the ZK
sigma transcript.  Neither pk_rec nor M_iss appears.

Recipient targeting (coupling).  L1-L5 bind E_iss to the key committed in Q but
do not by themselves prove that key is the *recipient's*.  That is closed in the
receipt verifier: combining this binding with the recipient's verifiable
decryption of E_iss (verifiable_decrypt -> M_named) and requiring M_named =
C_i - T forces ``pk_rec = Q's key`` algebraically.  See verify_receipt (note-a2).

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
from alberta_buck.wallet.transcript import keccak_scalar, keccak_raw
from alberta_buck.wallet.domains import FS_ISSUER_REENC, word as _word


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

    Carries the challenge ``e``, the four responses, and the five commitment
    points (so the Fiat-Shamir challenge can be recomputed).  The issuer also
    publishes the values ``Q``, ``U``, ``T`` (= T_hat) alongside the leaf; they
    are public inputs to :func:`issuer_reenc_verify`.
    """
    e:   int
    s_r: int
    s_b: int
    s_s: int
    s_g: int
    A1:  Tuple  # k_r*G
    A2:  Tuple  # k_r*H
    A3:  Tuple  # k_r*Q - k_b*U + k_g*G
    A4:  Tuple  # k_s*G
    A5:  Tuple  # k_s*R_reg + k_g*G
    Q:   Tuple  # pk_rec + beta*H            (blinded recipient key)
    U:   Tuple  # r'*H
    T:   Tuple  # T_hat = r'*pk_rec + gamma*G  (blinds M_iss = C_i - r'*pk_rec)


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

    # Published values.  T is blinded by gamma*G so M_iss = C_i - r'*pk_rec is
    # not recoverable as C_i - T (which would leak the private issuer's M).
    Q = add(pk_rec, mul(H_POINT, beta))                          # pk_rec + beta*H
    U = mul(H_POINT, r_prime % ORDER)                            # r'*H
    T = add(mul(pk_rec, r_prime % ORDER), mul(G1, gamma))        # r'*pk_rec + gamma*G

    # Y = C_reg + T - C_i = sk_iss*R_reg + gamma*G  (the L5 right-hand point).
    Y = add(C_reg, add(T, neg(C_i)))

    # Commitments (witnesses r', beta, sk_iss, gamma).
    k_r = rand_scalar(rng)
    k_b = rand_scalar(rng)
    k_s = rand_scalar(rng)
    k_g = rand_scalar(rng)
    A1 = mul(G1, k_r)                                            # k_r*G
    A2 = mul(H_POINT, k_r)                                       # k_r*H
    A3 = add(add(mul(Q, k_r), neg(mul(U, k_b))), mul(G1, k_g))   # k_r*Q - k_b*U + k_g*G
    A4 = mul(G1, k_s)                                            # k_s*G
    A5 = add(mul(R_reg, k_s), mul(G1, k_g))                      # k_s*R_reg + k_g*G

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
    if not eq(mul(H_POINT, s_r), add(proof.A2, mul(U, e))):
        return False
    # L3: s_r*Q - s_b*U + s_g*G == A3 + e*T   (=> T = r'*pk_rec + gamma*G)
    if not eq(add(add(mul(Q, s_r), neg(mul(U, s_b))), mul(G1, s_g)),
              add(proof.A3, mul(T, e))):
        return False
    # L4: s_s*G == A4 + e*pk_iss
    if not eq(mul(G1, s_s), add(proof.A4, mul(pk_iss, e))):
        return False
    # L5: s_s*R_reg + s_g*G == A5 + e*(C_reg + T - C_i)
    Y = add(C_reg, add(T, neg(C_i)))
    if not eq(add(mul(R_reg, s_s), mul(G1, s_g)), add(proof.A5, mul(Y, e))):
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
