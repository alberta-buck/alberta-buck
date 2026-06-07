"""Identity-targeted A1 Note -- addressed, *public* issuer.

Reference: alberta-buck-notes-identity-axis.org (the "one gadget", A1 row), and
the unilateral A2 corner it reuses (:mod:`alberta_buck.wallet.unilateral_a2`).

A1 and A2 are the *addressed* flavours; they differ only in whether the issuer is
public (A1) or private (A2).  The unified Identity-axis design makes their on-chain
*spend* identical -- the one gadget "prove a re-encryption of a registered Identity
under a target key" -- by a single substitution of what the note ciphertext
encrypts:

    A2:  eIss = (r'G,  M_I   + r'*M_rec)   -- the (private) issuer under M_rec
    A1:  eRec = (r'G,  M_rec + r'*M_rec)   -- the recipient's identity under itself

Decrypting A1's ``eRec`` under the recipient's identity scalar ``m_rec`` yields
``M_rec`` itself::

    C_e - m_rec*R_e = M_rec + r'*M_rec - m_rec*(r'G) = M_rec + r'*M_rec - r'*M_rec = M_rec

so the *same* deposit coupling (:func:`deposit_couple_prove`/`deposit_couple_verify`,
on chain ``verifyDepositCoupling``) proves the depositing account is bound to
``m_rec`` and that ``eRec`` decrypts under it to the point committed (blinded) in
``P_I = M_rec + b*H`` -- and the *same* membership proof certifies ``M_rec`` is a
registered Identity.  The issuer is public, so it is named directly off chain (its
registered ``M_iss`` + the batch Schnorr it signed at mint), not decrypted.

The payoff mirrors A2: the recipient -- any Fountain account bound to ``m_rec`` --
deposits while revealing no Identity, and produces a plaintext bilateral receipt
naming both the (public) issuer and itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from alberta_buck.wallet.bn254 import G1, ORDER, mul, eq, rand_scalar
from alberta_buck.wallet.elgamal import ElGamalCiphertext, elgamal_encrypt
from alberta_buck.wallet.notes import (
    FLAVOR_A1, NoteOpening, note_commitment,
)
from alberta_buck.wallet.verifiable_decrypt import (
    VDProof, verifiable_decrypt_prove, verifiable_decrypt_verify,
)
# Reuse the A2 gadgets verbatim -- A1's spend IS the A2 coupling + membership.
from alberta_buck.wallet.unilateral_a2 import (
    IdentityTree, RcptResult, a2_id_hash,
    deposit_couple_prove, deposit_couple_verify,           # noqa: F401 (re-export)
)


# ================================ Mint ======================================

@dataclass(frozen=True)
class MintedA1:
    """Everything the (public) issuer produces for one identity-targeted A1 note.

    ``eRec`` goes on chain (as the leaf-tie public output, like A2's ``eIss``); the
    full ``opening`` + ``eRec`` travel to the recipient off chain.  The issuer is
    public, so its Identity is *not* part of the note -- it is named at the batch
    level (the mint Schnorr + the registry record of ``msg.sender``).
    """
    eRec:    ElGamalCiphertext   # (r'*G, M_rec + r'*M_rec)  -- recipient identity under itself
    idHash:  int
    cm:      int
    opening: NoteOpening
    r_prime: int                 # issuer-held randomness (off chain)


def mint_unilateral_a1(
    M_rec,                        # recipient's identity POINT (learned out of band)
    v:       int,
    rho:     int,
    r_prime: Optional[int] = None,
    predicate: int = 0,
    rng=None,
) -> MintedA1:
    """Public issuer mints an identity-targeted A1 note addressed to identity ``M_rec``.

    Encrypts the recipient's identity ``M_rec`` under *itself*, so any Fountain
    account bound to ``m_rec`` can open and spend it via the shared deposit
    coupling.  No issuer secret is needed here -- the issuer is named publicly at
    mint, separately from the note.
    """
    r_prime = rand_scalar(rng) if r_prime is None else (r_prime % ORDER)

    # eRec = (r'*G, M_rec + r'*M_rec): the recipient identity under itself.
    eRec = elgamal_encrypt(M_rec, M_rec, r_prime)

    idHash = a2_id_hash(eRec)                      # Poseidon over the 4 ciphertext coords
    opening = NoteOpening(FLAVOR_A1, v, rho, idHash, predicate)
    cm = note_commitment(opening)
    return MintedA1(eRec=eRec, idHash=idHash, cm=cm, opening=opening, r_prime=r_prime)


# =============================== Receipt ====================================

@dataclass(frozen=True)
class A1Receipt:
    """A plaintext, third-party-checkable receipt the *recipient alone* produces.

    Names both identities (the public ``M_iss`` and the recipient ``M_rec``) and
    the value.  The recipient proves ``eRec`` decrypts under ``M_rec`` to ``M_rec``
    (so the note really addressed *their* identity), and both points are members of
    the registry-Identity tree.  The issuer is named publicly -- its ``M_iss`` is
    the registered Identity of the (public) minter; authorship of the batch is the
    mint Schnorr (alberta_buck.wallet.schnorr), checked at the mint-tx level.
    """
    M_iss:        Tuple          # issuer identity (public)
    M_rec:        Tuple          # recipient identity (= m_rec*G)
    value:        int
    eRec:         ElGamalCiphertext
    vd:           VDProof        # eRec decrypts under M_rec to M_rec
    issuer:       int            # issuer account (public minter)
    chainid:      int
    M_iss_member: bool
    M_rec_member: bool


def make_receipt_a1(
    m_rec:    int,
    minted:   MintedA1,
    M_iss,                        # the public issuer's registered identity point
    issuer:   int,
    chainid:  int,
    tree:     IdentityTree,
    rng=None,
) -> A1Receipt:
    """Recipient produces the A1 receipt unilaterally from ``m_rec`` and the note."""
    M_rec = mul(G1, m_rec % ORDER)
    eRec = minted.eRec
    # eRec decrypts under M_rec (key = m_rec*G) to M_rec; prove it without sk.
    vd = verifiable_decrypt_prove(eRec, m_rec, M_rec, issuer, chainid, rng=rng)
    return A1Receipt(
        M_iss=M_iss, M_rec=M_rec, value=minted.opening.v, eRec=eRec, vd=vd,
        issuer=issuer, chainid=chainid,
        M_iss_member=tree.contains(M_iss), M_rec_member=tree.contains(M_rec),
    )


def verify_receipt_a1(
    receipt:       A1Receipt,
    identity_root: int,
    tree:          IdentityTree,
) -> RcptResult:
    """Third-party verify, no secret.  VALID names (issuer M_iss, recipient M_rec,
    value) iff ``eRec`` decrypts under ``M_rec`` to ``M_rec`` and both identities
    are registered members."""
    eRec = receipt.eRec

    # (1) The recipient's verifiable decryption: eRec decrypts under M_rec to M_rec
    #     -- i.e. the note was addressed to this recipient's identity.
    if not verifiable_decrypt_verify(eRec, receipt.M_rec, receipt.M_rec, receipt.vd,
                                     receipt.issuer, receipt.chainid):
        return RcptResult(False, None, None, receipt.value, "verifiable decryption invalid")

    # (2) The recipient identity is registered (the spend membership gate).
    if not tree.contains(receipt.M_rec, identity_root):
        return RcptResult(False, None, None, receipt.value, "recipient M not a registered identity")

    # (3) The public issuer identity is registered.
    if not tree.contains(receipt.M_iss, identity_root):
        return RcptResult(False, None, None, receipt.value, "issuer M not a registered identity")

    return RcptResult(True, receipt.M_iss, receipt.M_rec, receipt.value, "VALID")


__all__ = [
    "MintedA1", "mint_unilateral_a1",
    "A1Receipt", "make_receipt_a1", "verify_receipt_a1",
    # re-exported A2 gadgets the A1 deposit reuses:
    "deposit_couple_prove", "deposit_couple_verify",
]
