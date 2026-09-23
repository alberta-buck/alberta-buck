"""B1 depositor binding -- the dual of the A2 issuer binding.

Reference: alberta-buck-notes.org ("Mutual Decryptability", B1 / identity-axis, "The Non-Deniable-Receipt Invariant", B1 depositor binding) and alberta-buck-notes-flow.org (B1 flows and depositor binding).

A B1 note is *bearer* (authorised by knowledge of the opening secret rho) from a
*public* issuer.  The recipient is unknown at mint, so the issuer-names-depositor
direction must be established at *spend*: the depositor reveals its Identity to the
issuer -- who is public -- without revealing it to Mallory.

The construction is the mirror image of :mod:`alberta_buck.wallet.unilateral_a2`,
with the roles swapped:

  A2 (recipient names issuer):  issuer encrypts M_I under the recipient's
                                receiving key pk_recv; the *recipient* decrypts
                                with its receiving secret k.
  B1 (issuer names depositor):  depositor encrypts M_dep under the issuer's public
                                key pk_iss; the *issuer* decrypts with sk_iss.

At deposit the depositor publishes ``E_dep_for_iss = (rG, M_dep + r*pk_iss)`` in
the SpentB event and proves -- on chain, hiding every Identity -- that it encrypts
a *registered* Identity the depositor controls:

    E4:  pk_dep        = sk_dep * G                 (the real payout-account key)
    E2:  C_d           = m_dep  * G + sk_dep * R_d   (account bound to M_dep = m_dep*G)
    F1:  R_f           = r * G                       (E_dep_for_iss randomness)
    F2:  C_f           = m_dep  * G + r * pk_iss      (E_dep_for_iss encrypts M_dep)

The shared witness ``m_dep`` couples E2 and F2: the Identity bound to the payout
account is exactly the one encrypted for the issuer.  A companion membership proof
of ``M_dep`` in the registry-Identity tree (the SNARK piece) makes a bogus
``E_dep_for_iss`` un-spendable.  The issuer then scans SpentB, decrypts with
``sk_iss``, and produces an *issuer-unilateral* receipt naming the depositor.

WHY B1 NEEDS NO FOLD, AND WHAT IT NEEDS INSTEAD.

The A-flavours had to fold their gate into one circuit because their two facts
rest on two DIFFERENT secrets -- the Identity scalar and the receiving key --
so no shared nonce could tie them and no choice of generator could repair it.
B1's two facts rest on ONE secret, ``m_dep``, which is why the sigma above is a
genuine tie and stays a sigma.

What B1 does need is an honest hiding generator.  The composition of this sigma
with the membership proof is an equality between two openings of the public
point ``P_dep``, and that equality only holds if nobody knows ``log_G(H)``.
With a known one, a depositor holding any registered identity scalar ``m'``
makes the membership half speak about ``m'`` while this sigma speaks about its
own ``m_dep`` -- and an unregistered depositor spends.  So ``P_dep`` is built on
:data:`alberta_buck.wallet.nums.H_PEDERSEN`, hashed to the curve rather than
multiplied out of ``G``.  :mod:`issuer_reenc` blinds on the same generator,
because the A2 fold opens its ``T`` again at spend and a known log would let a
minter pay any difference of Identities in the blind.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from alberta_buck.wallet.bn254 import (
    G1, ORDER, add, mul, neg, eq, rand_scalar, point_to_words,
)
from alberta_buck.wallet.elgamal import (
    ElGamalCiphertext, elgamal_encrypt, elgamal_decrypt,
)
from alberta_buck.wallet.transcript import keccak_scalar
from alberta_buck.wallet.verifiable_decrypt import (
    VDProof, verifiable_decrypt_prove, verifiable_decrypt_verify,
)
from alberta_buck.wallet.unilateral_a2 import IdentityTree, RcptResult
from alberta_buck.wallet.nums import H_PEDERSEN
from alberta_buck.wallet.domains import FS_DEPOSITOR_BINDING, word as _word


# ========================= Depositor binding ================================

@dataclass(frozen=True)
class DepositorBindingProof:
    """Five-relation Okamoto sigma for the B1 depositor binding.

    ``E_dep_for_iss`` is published (in the SpentB event) and is a public input;
    the proof attests it encrypts the depositor's registered Identity.  ``P_dep``
    is a perfectly-hiding Pedersen commitment to that same Identity ``M_dep``,
    published so the G1-tie membership proof can certify ``M_dep`` is a registered
    member *bound to this binding* -- the P relation below ties ``P_dep``'s scalar
    to the same ``m_dep`` as E2/F2 (shared response ``s_m``).
    """
    e:   int
    s_m: int      # response for m_dep (identity scalar)
    s_s: int      # response for sk_dep (payout-account key)
    s_r: int      # response for r (E_dep_for_iss randomness)
    s_b: int      # response for b (P_dep blind)
    A2:  Tuple    # k_m*G + k_s*R_d
    A4:  Tuple    # k_s*G
    B1:  Tuple    # k_r*G
    B2:  Tuple    # k_m*G + k_r*pk_iss
    A_p: Tuple    # k_m*G + k_b*H              (P-relation commitment)
    P_dep: Tuple  # M_dep + b*H                (blinded commitment of M_dep)


def _db_transcript(pk_dep, E_dep: ElGamalCiphertext, pk_iss,
                   eDepForIss: ElGamalCiphertext, A2, A4, B1, B2, A_p, P_dep,
                   account: int, chainid: int) -> int:
    pts = [pk_dep, E_dep.R, E_dep.C, pk_iss, eDepForIss.R, eDepForIss.C,
           A2, A4, B1, B2, A_p, P_dep]
    words: List[int] = []
    for P in pts:
        x, y = point_to_words(P)
        words.append(x)
        words.append(y)
    words.append(account)
    words.append(chainid)
    words.append(_word(FS_DEPOSITOR_BINDING))
    return keccak_scalar(*words)


def b1_bind_prove(
    m_dep:   int,
    sk_dep:  int,
    E_dep:   ElGamalCiphertext,   # depositor's registered credential (R_d, C_d)
    pk_iss,                       # public issuer's registered key
    account: int,                 # depositor account address (msg.sender at spend)
    chainid: int,
    r:       Optional[int] = None,
    b:       Optional[int] = None,
    rng=None,
) -> Tuple[DepositorBindingProof, ElGamalCiphertext]:
    """Build ``E_dep_for_iss`` and prove it encrypts the depositor's registered
    Identity under ``pk_iss``, all Identities hidden.  Also publishes ``P_dep =
    M_dep + b*H`` (blinded) so the membership proof can bind ``M_dep`` to this
    binding.  Returns (proof, E_dep_for_iss)."""
    pk_dep = mul(G1, sk_dep % ORDER)
    R_d, C_d = E_dep.R, E_dep.C
    M_dep = mul(G1, m_dep % ORDER)
    H = H_PEDERSEN

    # Sanity: the payout account must be bound to identity m_dep.
    assert eq(C_d, add(M_dep, mul(R_d, sk_dep % ORDER))), \
        "E_dep does not decrypt to m_dep*G under sk_dep"

    r = rand_scalar(rng) if r is None else (r % ORDER)
    eDepForIss = elgamal_encrypt(M_dep, pk_iss, r)        # (r*G, M_dep + r*pk_iss)

    b = rand_scalar(rng) if b is None else (b % ORDER)
    P_dep = add(M_dep, mul(H, b))                         # M_dep + b*H (membership commitment)

    k_m = rand_scalar(rng)
    k_s = rand_scalar(rng)
    k_r = rand_scalar(rng)
    k_b = rand_scalar(rng)
    A4 = mul(G1, k_s)                                     # k_s*G
    A2 = add(mul(G1, k_m), mul(R_d, k_s))                 # k_m*G + k_s*R_d
    B1 = mul(G1, k_r)                                     # k_r*G
    B2 = add(mul(G1, k_m), mul(pk_iss, k_r))              # k_m*G + k_r*pk_iss
    A_p = add(mul(G1, k_m), mul(H, k_b))                  # k_m*G + k_b*H

    e = _db_transcript(pk_dep, E_dep, pk_iss, eDepForIss, A2, A4, B1, B2, A_p, P_dep,
                       account, chainid)
    s_m = (k_m + e * (m_dep % ORDER)) % ORDER
    s_s = (k_s + e * (sk_dep % ORDER)) % ORDER
    s_r = (k_r + e * r) % ORDER
    s_b = (k_b + e * b) % ORDER
    return (DepositorBindingProof(e=e, s_m=s_m, s_s=s_s, s_r=s_r, s_b=s_b,
                                  A2=A2, A4=A4, B1=B1, B2=B2, A_p=A_p, P_dep=P_dep),
            eDepForIss)


def b1_bind_verify(
    pk_dep,
    E_dep:      ElGamalCiphertext,
    pk_iss,
    eDepForIss: ElGamalCiphertext,
    proof:      DepositorBindingProof,
    account:    int,
    chainid:    int,
) -> bool:
    """Verify the B1 depositor binding.  True iff ``E_dep_for_iss`` encrypts, under
    ``pk_iss``, the Identity bound to the payout account -- revealing nothing."""
    R_d, C_d = E_dep.R, E_dep.C
    R_f, C_f = eDepForIss.R, eDepForIss.C
    e, s_m, s_s, s_r, s_b = proof.e, proof.s_m, proof.s_s, proof.s_r, proof.s_b
    H = H_PEDERSEN

    # E4: s_s*G == A4 + e*pk_dep
    if not eq(mul(G1, s_s), add(proof.A4, mul(pk_dep, e))):
        return False
    # E2: s_m*G + s_s*R_d == A2 + e*C_d
    if not eq(add(mul(G1, s_m), mul(R_d, s_s)), add(proof.A2, mul(C_d, e))):
        return False
    # F1: s_r*G == B1 + e*R_f
    if not eq(mul(G1, s_r), add(proof.B1, mul(R_f, e))):
        return False
    # F2: s_m*G + s_r*pk_iss == B2 + e*C_f
    if not eq(add(mul(G1, s_m), mul(pk_iss, s_r)), add(proof.B2, mul(C_f, e))):
        return False
    # P:  s_m*G + s_b*H == A_p + e*P_dep   (P_dep = m_dep*G + b*H, same m_dep)
    if not eq(add(mul(G1, s_m), mul(H, s_b)), add(proof.A_p, mul(proof.P_dep, e))):
        return False
    # Fiat-Shamir
    return proof.e == _db_transcript(pk_dep, E_dep, pk_iss, eDepForIss,
                                     proof.A2, proof.A4, proof.B1, proof.B2,
                                     proof.A_p, proof.P_dep, account, chainid)


# ====================== Issuer-unilateral receipt ===========================

@dataclass(frozen=True)
class IssuerReceipt:
    """A plaintext receipt the *issuer alone* produces: it decrypts the depositor's
    Identity from the event and names both parties.  Dual of
    :class:`alberta_buck.wallet.unilateral_a2.UnilateralReceipt`."""
    M_iss:      Tuple            # issuer identity (public)
    M_dep:      Tuple            # depositor identity (decrypted from the event)
    value:      int
    pk_iss:     Tuple
    eDepForIss: ElGamalCiphertext
    vd:         VDProof          # E_dep_for_iss decrypts under pk_iss to M_dep
    issuer:     int
    chainid:    int
    M_dep_member: bool


def make_issuer_receipt(
    sk_iss:     int,
    M_iss,                        # issuer's own (public) identity point
    eDepForIss: ElGamalCiphertext,
    value:      int,
    issuer:     int,
    chainid:    int,
    tree:       IdentityTree,
    rng=None,
) -> IssuerReceipt:
    """Issuer scans the SpentB event, decrypts the depositor's Identity, and proves
    the decryption -- unilaterally, from ``sk_iss`` alone."""
    pk_iss = mul(G1, sk_iss % ORDER)
    M_dep = elgamal_decrypt(eDepForIss, sk_iss)
    vd = verifiable_decrypt_prove(eDepForIss, sk_iss, M_dep, issuer, chainid, rng=rng)
    return IssuerReceipt(
        M_iss=M_iss, M_dep=M_dep, value=value, pk_iss=pk_iss,
        eDepForIss=eDepForIss, vd=vd, issuer=issuer, chainid=chainid,
        M_dep_member=tree.contains(M_dep),
    )


def verify_issuer_receipt(
    receipt:       IssuerReceipt,
    identity_root: int,
    tree:          IdentityTree,
) -> RcptResult:
    """Third-party verify with no secret.  VALID names (issuer M_iss, depositor
    M_dep, value) iff the issuer's decryption is correct and the recovered
    depositor Identity is registered."""
    # (1) The issuer's verifiable decryption of the event ciphertext.
    if not verifiable_decrypt_verify(receipt.eDepForIss, receipt.pk_iss,
                                     receipt.M_dep, receipt.vd,
                                     receipt.issuer, receipt.chainid):
        return RcptResult(False, None, None, receipt.value, "verifiable decryption invalid")

    # (2) The depositor Identity is registered -- the collusion check (a bogus
    #     E_dep_for_iss decrypts to a non-member and is rejected here / at spend).
    if not tree.contains(receipt.M_dep, identity_root):
        return RcptResult(False, None, None, receipt.value, "depositor M not a registered identity")

    # (3) The issuer's own (public) Identity is registered.
    if not tree.contains(receipt.M_iss, identity_root):
        return RcptResult(False, None, None, receipt.value, "issuer M not a registered identity")

    # Naming convention: RcptResult.issuer_M holds the *issuer*, recipient_M the
    # depositor (the party named at deposit).
    return RcptResult(True, receipt.M_iss, receipt.M_dep, receipt.value, "VALID")


__all__ = [
    "DepositorBindingProof", "b1_bind_prove", "b1_bind_verify",
    "IssuerReceipt", "make_issuer_receipt", "verify_issuer_receipt",
]
