"""Off-chain non-deniable-receipt verifiers for BUCK payments.

Reference: alberta-buck-notes.org ("The Non-Deniable-Receipt Invariant") and alberta-buck-receipt.org (AB-RCPT/1 receipts as independently verifiable proofs naming the registered counterparty).  Two payment paths, two receipt shapes, one verification idiom -- a
party assembles a receipt from data they hold plus public chain state, and any
third party re-checks it with no secret, naming the counterparty's registered
Identity:

* :class:`Receipt` / :func:`receipt_verify` -- a *Notes* payment from a public
  issuer (A1, B1).  The issuer's Identity is in the clear, so the named M comes
  straight from the registry plus the publicly-checkable batch Schnorr.

* :class:`ApproveReceipt` / :func:`approve_receipt_verify` -- a *direct
  Identity-bound EOA transfer*.  Here the counterparty's Identity lives only
  inside a ciphertext re-encrypted to the recipient, so naming it needs the
  approve handshake (Chaum-Pedersen, soundness) plus a *verifiable decryption*
  (:mod:`alberta_buck.wallet.verifiable_decrypt`) that reveals M and proves it
  is the correct decryption -- the "no deniability under collusion" clause.

Both verifiers share :class:`RegisteredIdentity` (the registry read) and
:class:`RcptResult` (the outcome).

For the Notes :class:`Receipt`, the chain has four links, each checkable
against public chain state::

    (a) OPENING   cm = Poseidon5(flavor, v, rho, idHash, predicate)
    (b) MINTED    cm appears in the mint tx calldata cms[]
    (c) ISSUER    the public issuer's registered key signed keccak256(cms[]),
                  so every leaf in the batch is bound to their Identity M_iss
    (d) PAID      nullifier = Poseidon3(rho, idHash, tag) was burned in a
                  Spent* event delivering `face` to `recipient`

This module covers the **public-issuer flavors (A1, B1)** -- the Phase 1
guarantee.  Link (c) for them reduces to a registry read (the issuer's M is
in the clear) plus the verified batch Schnorr (Notes mutual-decryptability,
Phase 1).  The private-issuer flavor A2 binds the issuer *in-SNARK* at mint
(Phase 2, not yet shipped); :func:`receipt_verify` rejects an A2 receipt with a
clear reason rather than pretending to name an unverified issuer.

Scope note: the verifier checks the *cryptographic* links it can evaluate from
the receipt's data -- (a) the opening recomputes ``cm`` and it lies in the
batch, (c) the batch Schnorr verifies under the registry-read public key, and
(d) the nullifier and face are the deterministic functions of the opening.  The
"``cm`` is in *this* mint tx" and "the nullifier was burned in *this* Spent
event" anchors are chain reads the caller supplies as receipt fields (exactly
the data a third party re-derives from chain history); this module does not
itself read the chain, matching the rest of :mod:`alberta_buck.wallet`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

from alberta_buck.wallet.notes import (
    NoteOpening,
    FLAVOR_A1,
    FLAVOR_A2,
    FLAVOR_B1,
    note_commitment,
    nullifier_a,
    nullifier_b,
)
from alberta_buck.wallet.schnorr import (
    SchnorrProof,
    batch_commitment,
    issuer_schnorr_verify,
)
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.chaum_pedersen import CPProof, chaum_pedersen_verify
from alberta_buck.wallet.verifiable_decrypt import VDProof, verifiable_decrypt_verify


@dataclass(frozen=True)
class RegisteredIdentity:
    """An IdentityRegistry record as read by a receipt verifier.

    Mirrors the on-chain state the registry exposes: ``is_public`` is
    ``isPublicIdentity[addr]``, ``pk`` is the registered identity key
    ``_pk[addr]``, and ``E_addr`` is the registered credential ciphertext
    ``_E_addr[addr]``.  ``M`` is the decrypted Identity point -- known in the
    clear only for a *public* identity (the off-chain-attested ``m * G``), which
    is what the Notes :func:`receipt_verify` names directly; for an
    encrypted-identity EOA party ``M`` is ``None`` (it is recovered via the
    approve receipt's verifiable decryption instead) and ``E_addr`` is the field
    the approve handshake is checked against.
    """
    is_public: bool
    pk: Tuple                                  # G1: pk = sk * G  (registered)
    M:  Optional[Tuple] = None                 # G1: in-clear Identity, public idents
    E_addr: Optional[ElGamalCiphertext] = None # registered credential ciphertext


@dataclass(frozen=True)
class Receipt:
    """A non-deniable receipt for one Note payment from a public issuer.

    Assembled by the recipient from the note ``opening`` (link a), the mint
    transaction's ``cms`` calldata and ``issuer_sig`` Schnorr + ``issuer``
    address (links b, c), and the ``Spent*`` event's ``nullifier``/``face``/
    ``recipient`` (link d).  ``chainid`` is the chain the binding was signed on.
    """
    opening:    NoteOpening
    cms:        Tuple[int, ...]   # full minted batch; `opening`'s cm must be in it
    issuer:     int               # issuer address as uint160
    issuer_sig: SchnorrProof      # Schnorr over keccak256(abi.encodePacked(cms))
    chainid:    int
    nullifier:  int               # from the Spent* event
    face:       int               # value delivered, from the Spent* event
    recipient:  int               # payee address, from the Spent* event


@dataclass(frozen=True)
class RcptResult:
    """Outcome of :func:`receipt_verify`.

    On success ``ok`` is True and ``identity_M`` names the payer's registered
    Identity point with ``value`` the amount paid; ``reason`` is empty.  On
    failure ``ok`` is False and ``reason`` names the failing link.
    """
    ok:          bool
    identity_M:  Optional[Tuple]
    value:       Optional[int]
    reason:      str = ""


def _nullifier_for(opening: NoteOpening) -> int:
    """Deterministic nullifier of an opening, dispatched on flavor tag.

    A-flavor (A1/A2) uses tag 4243, B-flavor (B1) uses tag 4242 -- the same
    domain separation the spend circuits enforce.
    """
    if opening.flavor in (FLAVOR_A1, FLAVOR_A2):
        return nullifier_a(opening.rho, opening.id_hash)
    return nullifier_b(opening.rho, opening.id_hash)


def receipt_verify(
    receipt:  Receipt,
    registry: Mapping[int, RegisteredIdentity],
) -> RcptResult:
    """Verify a public-issuer receipt, naming the payer's registered Identity.

    ``registry`` maps an issuer address to its :class:`RegisteredIdentity` (the
    on-chain registry read).  Returns :class:`RcptResult`; on success
    ``identity_M`` is the issuer's Identity point ``M_iss`` and ``value`` the
    paid ``face``.  The check is collusion-resistant: every spendable
    public-issuer leaf carries a batch Schnorr (mint reverts otherwise), so a
    payer cannot produce an accepted payment for which no naming receipt exists.
    """
    o = receipt.opening

    # A2 is the addressed *private*-issuer flavor: no batch Schnorr binds it
    # (the issuer is bound in-SNARK at mint, Phase 2).  A public-issuer receipt
    # cannot name it -- refuse rather than name an unverified Identity.
    if o.flavor == FLAVOR_A2:
        return RcptResult(False, None, None,
                          "(c) A2 private-issuer receipt requires Phase 2 in-SNARK binding")

    # (a) OPENING: recompute cm from the opening and require it in the batch.
    cm = note_commitment(o)
    if cm not in receipt.cms:
        return RcptResult(False, None, None, "(a) opening cm not in minted batch")

    # (c) ISSUER: read the registry; the issuer must be a public Identity (the
    # only case whose M is recoverable without the recipient-side ciphertext).
    rec = registry.get(receipt.issuer)
    if rec is None:
        return RcptResult(False, None, None, "(c) issuer not registered")
    if not rec.is_public:
        return RcptResult(False, None, None, "(c) issuer is not a public Identity")
    if rec.M is None:
        return RcptResult(False, None, None, "(c) public issuer Identity M unavailable")

    # (b)+(c): the batch Schnorr binds the issuer's registered key to keccak of
    # the whole batch, hence (via link a) to this leaf.  This is exactly
    # IdentityRegistry.verifyIssuerSchnorr's algebraic + Fiat-Shamir check.
    h_batch = batch_commitment(receipt.cms)
    if not issuer_schnorr_verify(rec.pk, receipt.issuer_sig,
                                 h_batch, receipt.issuer, receipt.chainid):
        return RcptResult(False, None, None, "(b)/(c) issuer batch binding fails")

    # (d) PAID: nullifier and face are deterministic functions of the opening.
    if _nullifier_for(o) != receipt.nullifier:
        return RcptResult(False, None, None, "(d) nullifier does not match opening")
    if receipt.face != o.v:
        return RcptResult(False, None, None, "(d) paid face != note value")

    return RcptResult(True, rec.M, o.v, "")


# ---- direct Identity-bound EOA transfer ------------------------------------

@dataclass(frozen=True)
class ApproveReceipt:
    """A non-deniable receipt for a direct Identity-bound EOA transfer.

    The recipient (``spender``) assembles it from the approve handshake the
    counterparty (``sender``) published -- ``E_for_spender``, a re-encryption of
    the sender's registered Identity under the spender's key, plus the sender's
    Chaum-Pedersen ``cp_proof`` (= IdentityRegistry.verifyApprove) -- together
    with the spender's own ``vd_proof`` revealing the decrypted ``M_named``.

    The two proofs compose to name the sender soundly and non-deniably:
    ``cp_proof`` binds ``E_for_spender`` to the sender's *registered* Identity
    (whatever it is, without revealing it), and ``vd_proof`` proves ``M_named``
    is exactly what ``E_for_spender`` decrypts to under the spender's registered
    key -- so a third party, not just the spender, learns the named Identity.
    """
    sender:        int                # counterparty being named (the payer)
    spender:       int                # recipient assembling the receipt
    chainid:       int
    E_for_spender: ElGamalCiphertext  # sender's re-encryption of M under pk_spender
    cp_proof:      CPProof            # sender's approve handshake (soundness)
    M_named:       Tuple              # the revealed Identity point of `sender`
    vd_proof:      VDProof            # spender's verifiable decryption -> M_named
    registry_addr: int                 # registry domain used by the approve proof


def approve_receipt_verify(
    receipt:  ApproveReceipt,
    registry: Mapping[int, RegisteredIdentity],
) -> RcptResult:
    """Verify an EOA approve receipt, naming the sender's registered Identity.

    ``registry`` maps an address to its :class:`RegisteredIdentity`; the sender
    record must carry ``E_addr`` (the registered credential the approve
    handshake is checked against).  On success ``identity_M`` is the sender's
    Identity point; ``value`` is ``None`` (the approve receipt names the
    counterparty -- the transferred amount is the separate ERC-20 Transfer log).
    """
    snd = registry.get(receipt.sender)
    spn = registry.get(receipt.spender)
    if snd is None or snd.E_addr is None:
        return RcptResult(False, None, None, "(c) sender not registered")
    if spn is None:
        return RcptResult(False, None, None, "(c) spender not registered")

    # Soundness: the sender re-encrypted their *registered* Identity for the
    # spender (verifyApprove).  Read E_addr + pks from the registry so the
    # receipt cannot substitute a different "registered" record.
    if not chaum_pedersen_verify(
        snd.E_addr, receipt.E_for_spender, snd.pk, spn.pk,
        receipt.cp_proof, receipt.sender, receipt.spender, receipt.chainid,
        receipt.registry_addr,
    ):
        return RcptResult(False, None, None, "(soundness) approve handshake fails")

    # Recovery + provability: M_named is exactly what E_for_spender decrypts to
    # under the spender's registered key -- publicly checkable, no sk revealed.
    if not verifiable_decrypt_verify(
        receipt.E_for_spender, spn.pk, receipt.M_named, receipt.vd_proof,
        receipt.spender, receipt.chainid,
    ):
        return RcptResult(False, None, None, "(recovery) verifiable decryption fails")

    return RcptResult(True, receipt.M_named, None, "")


__all__ = [
    "RegisteredIdentity",
    "Receipt",
    "RcptResult",
    "receipt_verify",
    "ApproveReceipt",
    "approve_receipt_verify",
]
