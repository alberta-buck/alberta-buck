"""Identity-targeted A1 Note -- addressed, *public* issuer.

Reference: alberta-buck-notes.org ("Mutual Decryptability", "one gadget", A1 row), and
the unilateral A2 corner it reuses (:mod:`alberta_buck.wallet.unilateral_a2`).

A1 and A2 are the *addressed* flavours; they differ only in whether the issuer is
public (A1) or private (A2).  The unified Identity-axis design makes their on-chain
*spend* identical -- the one gadget "prove a re-encryption of a registered Identity
under a target key" -- by a single substitution of what the note ciphertext
encrypts:

    A2:  eIss = (r'G,  M_I   + r'*pk_recv)   -- the (private) issuer, to the mailbox
    A1:  eRec = (r'G,  M_rec + r'*pk_recv)   -- the recipient's Identity, to the mailbox

Both are keyed to the recipient's registered *receiving key*
``pk_recv = k*G``, never to the Identity point: an identity scalar is a read
capability the design discloses to every counterparty, so it cannot also be a
decryption key (:mod:`alberta_buck.wallet.recvkey`).  An addressed note
therefore *names* an Identity and is *keyed* to that Identity's mailbox, and
the two are different objects.

Decrypting A1's ``eRec`` under the receiving secret ``k`` yields ``M_rec``::

    C_e - k*R_e = M_rec + r'*pk_recv - k*(r'G) = M_rec + r'*kG - r'*kG = M_rec

so the *same* folded deposit gate (:mod:`alberta_buck.wallet.deposit_fold`)
proves that ``k`` decrypts the note to the point committed in ``P``, that the
depositing account's credential holds ``M_rec``, and -- the relation a
single-secret design would get for free -- that a registered accumulator leaf
commits the pair ``(M_rec, k*G)``.  The issuer is public, so it is named
directly off chain (its registered ``M_iss`` + the batch Schnorr it signed at
mint), not decrypted.

The payoff mirrors A2: the recipient -- any Fountain account bound to
``m_rec`` -- deposits while revealing no Identity, and produces a plaintext
bilateral receipt naming both the (public) issuer and itself.
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
from alberta_buck.wallet.unilateral_a2 import IdentityTree, RcptResult


# ================================ Mint ======================================

@dataclass(frozen=True)
class MintedA1:
    """Everything the (public) issuer produces for one identity-targeted A1 note.

    ``eNote`` encrypts the note value ``v`` and ``eRec`` the recipient Identity,
    both to the recipient's receiving key ``pk_recv``.  ``idHash`` commits to
    ``(eNote, m_issuer, sigma_R, sigma_s)`` via Poseidon8 (matching
    ``id_hash_a1``), binding the note to the issuer identity it names.  The pair
    ``(sigma_R, sigma_s)`` is opaque issuer material: no circuit, contract or verifier
    checks it.  What authenticates a public issuer is the batch Schnorr its account
    signs at mint, which covers this note's commitment.  ``eRec`` goes on chain
    (as the leaf-tie public output); the full ``opening`` + ``eNote`` + ``eRec``
    travel to the recipient off chain.
    """
    eNote:   ElGamalCiphertext   # (r_n*G, v*G + r_n*pk_recv)   -- value, to the mailbox
    eRec:    ElGamalCiphertext   # (r'*G, M_rec + r'*pk_recv)   -- Identity named, to the mailbox
    idHash:  int
    cm:      int
    opening: NoteOpening
    r_prime: int                 # issuer-held randomness (off chain)
    r_note:  int                 # note-value encryption randomness (off chain)


def mint_unilateral_a1(
    M_rec,                        # recipient's identity POINT: the PLAINTEXT of eRec
    pk_recv,                      # recipient's RECEIVING key: the ENCRYPTION key
    v:       int,
    rho:     int,
    m_issuer: int,                # issuer's registered identity scalar
    sigma_R,                      # opaque issuer material (unchecked; see MintedA1)
    sigma_s: int,                 # opaque issuer material (unchecked; see MintedA1)
    r_prime: Optional[int] = None,
    predicate: int = 0,
    rng=None,
) -> MintedA1:
    """Public issuer mints an A1 note naming ``M_rec``, keyed to ``pk_recv``.

    Two points, two jobs.  ``M_rec`` is the Identity the note names: it is the
    plaintext of ``eRec``, and it is what the receipt reports and the deposit
    gate's credential relation matches.  ``pk_recv`` is the recipient's
    registered receiving key: it is what both ciphertexts are encrypted to, and
    what the receiving secret ``k`` opens.  Collapsing them -- encrypting
    ``M_rec`` under ``M_rec`` -- would leave ``C = m(G+R)``, where message and
    key share a secret and one scalar multiplication per candidate identifies
    the recipient from public calldata.

    The payer learns ``pk_recv`` on the same out-of-band channel that carries
    ``M_rec``, and SHOULD check the recipient's binding
    (:func:`alberta_buck.wallet.recvkey.verify_receiving_binding`) before
    minting: that is what assures it the mailbox belongs to the Identity it
    means to pay.  Nothing is needed from the recipient at payment time.

    ``idHash`` commits to ``(eNote, m_issuer, sigma_R, sigma_s)`` via Poseidon8,
    binding the note to the public issuer.
    """
    from alberta_buck.wallet.notes import id_hash_a1

    r_prime = rand_scalar(rng) if r_prime is None else (r_prime % ORDER)

    # eNote = (r_n*G, v*G + r_n*pk_recv): the value, keyed to the mailbox.
    r_note = rand_scalar(rng)
    eNote = elgamal_encrypt(mul(G1, v), pk_recv, r_note)

    # eRec = (r'*G, M_rec + r'*pk_recv): the Identity NAMED in the plaintext,
    # keyed to the receiving key.  Only the holder of k reads it; anyone holding
    # m_rec -- which every counterparty does -- reads nothing.
    eRec = elgamal_encrypt(M_rec, pk_recv, r_prime)

    idHash = id_hash_a1(eNote, m_issuer, sigma_R, sigma_s)
    opening = NoteOpening(FLAVOR_A1, v, rho, idHash, predicate)
    cm = note_commitment(opening)
    return MintedA1(eNote=eNote, eRec=eRec, idHash=idHash, cm=cm,
                    opening=opening, r_prime=r_prime, r_note=r_note)


# =============================== Receipt ====================================

@dataclass(frozen=True)
class A1Receipt:
    """A plaintext, third-party-checkable receipt the *recipient alone* produces.

    Names both identities (the public ``M_iss`` and the recipient ``M_rec``) and
    the value.  The recipient proves ``eRec`` decrypts under its receiving key
    ``pk_recv`` to ``M_rec`` -- so the note really named *their* Identity, and
    only the holder of the receiving secret could say so -- and both points are
    members of the registry-Identity tree.  The issuer is named publicly: its
    ``M_iss`` is the registered Identity of the (public) minter, and authorship
    of the batch is the mint Schnorr (alberta_buck.wallet.schnorr), checked at
    the mint-tx level.

    Carrying ``pk_recv`` in the clear costs nothing.  The receipt already names
    both parties, and the key alone decides no addressing: testing whether some
    other ciphertext is keyed to it, without the secret, is a DDH decision.
    """
    M_iss:        Tuple          # issuer identity (public)
    M_rec:        Tuple          # recipient identity (= m_rec*G), NAMED
    pk_recv:      Tuple          # recipient receiving key (= k*G), the VD key
    value:        int
    eRec:         ElGamalCiphertext
    vd:           VDProof        # eRec decrypts under pk_recv to M_rec
    issuer:       int            # issuer account (public minter)
    chainid:      int
    M_iss_member: bool
    M_rec_member: bool


def make_receipt_a1(
    k_recv:   int,                # the RECEIVING secret: what decrypts eRec
    M_rec,                        # the recipient's Identity POINT: what is named
    minted:   MintedA1,
    M_iss,                        # the public issuer's registered identity point
    issuer:   int,
    chainid:  int,
    tree:     IdentityTree,
    rng=None,
) -> A1Receipt:
    """Recipient produces the A1 receipt unilaterally from ``k_recv`` and the note.

    Takes the Identity separately from the secret it decrypts with, because
    those are now two values.  Deriving one from the other is exactly the
    collapse the receiving key exists to prevent, so this signature is the
    shape of the fix rather than an inconvenience.
    """
    eRec = minted.eRec
    # eRec decrypts under pk_recv (key = k*G) to M_rec; prove it without sk.
    vd = verifiable_decrypt_prove(eRec, k_recv, M_rec, issuer, chainid, rng=rng)
    return A1Receipt(
        M_iss=M_iss, M_rec=M_rec, pk_recv=mul(G1, k_recv % ORDER),
        value=minted.opening.v, eRec=eRec, vd=vd,
        issuer=issuer, chainid=chainid,
        M_iss_member=tree.contains(M_iss), M_rec_member=tree.contains(M_rec),
    )


def verify_receipt_a1(
    receipt:       A1Receipt,
    identity_root: int,
    tree:          IdentityTree,
) -> RcptResult:
    """Third-party verify, no secret.  VALID names (issuer M_iss, recipient M_rec,
    value) iff ``eRec`` decrypts under the recipient's receiving key to ``M_rec``
    and both identities are registered members."""
    eRec = receipt.eRec

    # (1) The recipient's verifiable decryption: eRec decrypts under pk_recv to
    #     M_rec -- the note named this Identity, and only the holder of the
    #     receiving secret can produce this proof.
    if not verifiable_decrypt_verify(eRec, receipt.pk_recv, receipt.M_rec, receipt.vd,
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
]
