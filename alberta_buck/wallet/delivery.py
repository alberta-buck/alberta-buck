"""The delivery: what an addressed note carries from minter to recipient.

A Note is minted on chain as a commitment and travels off chain as a delivery.  The recipient must
be able to spend from the delivery alone, and the channel that carries it -- chosen by the minter,
and not the chain -- must learn nothing from it.  Those two requirements decide every field.

What must travel.  The recipient recomputes the commitment, so it needs the opening (the face
``v``, the nullifier seed ``rho``, the predicate) and the ciphertexts ``idHash`` commits.  Its
spend proves the note tie, and the fold's witness is the SCALAR ``r*k``, which the point ``k*R``
does not give -- so the mint randomness travels too.  An A2 recipient also proves the issuer's
Identity registered, under the salt of the issuer's naming association, and opens the mint
binding's ``T = r'*pk_recv + gamma*H``, which ``idHash`` commits, so it needs ``gamma``.

What the channel must not learn, and would, in the clear:

  ``v``         the face, while the note is in flight
  ``rho``       with ``idHash`` it is the nullifier, so the channel would learn WHEN the note is spent
  ``r'``        ``M = C - r'*pk_recv``: the issuer's Identity, read without ``k``
  ``gamma``     ``M = C - T + gamma*H``: the same Identity, by the other road
  ``salt_iss``  a scan of the issuer's naming leaf against every identity the channel knows

So every secret scalar travels WRAPPED to the mailbox key.  One shared point per delivery, from the
value ciphertext both flavours carry --

    minter     S = r_note * pk_recv
    recipient  S = k * eNote.R

-- and one mask per field, keyed by the field's name (:func:`alberta_buck.wallet.recvkey.wrap_mask`),
because one pad over several scalars leaks their differences.  The ciphertexts, ``T`` (A2), the
issuer's Schnorr signature (A1) and the predicate travel in the clear: none opens without ``k``.

A bearer (B1) note has no delivery in this sense.  Its opening IS the note, handed to whoever
should be able to spend it, and wrapping it to a key would make it addressed.

Opening a delivery checks what the wrap cannot: that the unwrapped randomness really is the
randomness of the ciphertexts it arrived with, that ``k`` opens the value ciphertext to the stated
face, and for A2 that ``T`` opens to this mailbox.  A delivery addressed to someone else, or corrupted in transit, is refused here with
a reason, not discovered later as a spend that will not prove.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from alberta_buck.wallet.bn254 import G1, add, eq, mul, point_to_words, words_to_point
from alberta_buck.wallet.elgamal import ElGamalCiphertext, elgamal_decrypt
from alberta_buck.wallet.nums import H_PEDERSEN
from alberta_buck.wallet.notes import (
    FLAVOR_A1, FLAVOR_A2, NoteOpening, id_hash_a1, id_hash_a2, note_commitment,
)
from alberta_buck.wallet.recvkey import (
    mailbox_shared_minter, mailbox_shared_recipient, unwrap_scalar, wrap_scalar,
)

__all__ = [
    "DeliveryRefused",
    "OpenedA1", "OpenedA2",
    "deliver_a1", "deliver_a2",
    "open_a1", "open_a2",
]


class DeliveryRefused(ValueError):
    """A delivery that is not a spendable note for this mailbox key."""


# ---- serialization: decimal strings, the convention of every fixture ----------------------------

def _pt(P) -> Dict[str, str]:
    x, y                        = point_to_words(P)
    return {"x": str(x), "y": str(y)}


def _ct(c: ElGamalCiphertext) -> Dict[str, Any]:
    return {"R": _pt(c.R), "C": _pt(c.C)}


def _pt_of(d) -> Tuple:
    return words_to_point(int(d["x"]), int(d["y"]))


def _ct_of(d) -> ElGamalCiphertext:
    return ElGamalCiphertext(_pt_of(d["R"]), _pt_of(d["C"]))


# ---- the opened note ------------------------------------------------------------------------------

@dataclass(frozen=True)
class OpenedA1:
    """An A1 delivery opened by its recipient: everything the folded gate's witness needs from it."""
    opening:                    NoteOpening
    cm:                         int
    eNote:                      ElGamalCiphertext
    eRec:                       ElGamalCiphertext
    r_note:                     int


@dataclass(frozen=True)
class OpenedA2:
    """An A2 delivery opened by its recipient.  ``M_I`` is what ``k`` opens ``eIss`` to -- the
    issuer the recipient will name, and the Identity relation (5) proves registered.  ``T`` and
    ``gamma`` are the mint binding's blinded point and its blind, which the fold ties to ``k``."""
    opening:                    NoteOpening
    cm:                         int
    eNote:                      ElGamalCiphertext
    eIss:                       ElGamalCiphertext
    M_I:                        Tuple
    r_prime:                    int
    salt_iss:                   int
    T:                          Tuple
    gamma:                      int


# ---- the minter's side ----------------------------------------------------------------------------

def _wrapped(S, **fields: int) -> Dict[str, str]:
    """Each field masked under its own label -- ``rho`` becomes ``rhoWrapped``, and so on."""
    return {f"{name}Wrapped": str(wrap_scalar(val, S, name.encode())) for name, val in fields.items()}


def deliver_a1(minted, pk_recv) -> Dict[str, Any]:
    """The delivery for an A1 note, from what the minter holds.

    Args:
        minted: The :class:`alberta_buck.wallet.unilateral_a1.MintedA1`.
        pk_recv: The recipient's mailbox key the note was minted to.

    ``eRec``'s randomness does not travel: the recipient opens ``eRec`` with ``k``, and only an
    issuer-side receipt needs ``r'``, which the issuer keeps.
    """
    o                           = minted.opening
    S                           = mailbox_shared_minter(minted.r_note, pk_recv)
    return {
        "flavor":               FLAVOR_A1,
        "predicate":            str(o.predicate),
        "eNote":                _ct(minted.eNote),
        "eRec":                 _ct(minted.eRec),
        **_wrapped(S, rho=o.rho, v=o.v, rNote=minted.r_note),
    }


def deliver_a2(minted, pk_recv) -> Dict[str, Any]:
    """The delivery for an A2 note, from what the minter holds.

    Args:
        minted: The :class:`alberta_buck.wallet.unilateral_a2.MintedA2`, minted with the salt of
            the issuer's NAMING association (``salt_iss``).  Without that salt the recipient cannot
            prove the issuer registered, and the note cannot be spent.
        pk_recv: The recipient's mailbox key the note was minted to.
    """
    if minted.salt_iss is None:
        raise ValueError("an A2 delivery needs the issuer's naming salt (mint with salt_iss)")
    o                           = minted.opening
    S                           = mailbox_shared_minter(minted.r_note, pk_recv)
    return {
        "flavor":               FLAVOR_A2,
        "predicate":            str(o.predicate),
        "eNote":                _ct(minted.eNote),
        "eIss":                 _ct(minted.eIss),
        "T":                    _pt(minted.binding.T),
        **_wrapped(S, rho=o.rho, v=o.v, rPrime=minted.r_prime, saltIss=minted.salt_iss,
                   gamma=minted.gamma),
    }


# ---- the recipient's side -------------------------------------------------------------------------

def _unwrap(delivery: Dict[str, Any], S, *names: str) -> Dict[str, int]:
    try:
        return {n: unwrap_scalar(int(delivery[f"{n}Wrapped"]), S, n.encode()) for n in names}
    except KeyError as exc:
        raise DeliveryRefused(f"the delivery is missing {exc.args[0]}") from None


def _check_value(eNote: ElGamalCiphertext, k: int, v: int, r_note: Optional[int]) -> None:
    if r_note is not None and not eq(eNote.R, mul(G1, r_note)):
        raise DeliveryRefused("the unwrapped r_note is not eNote's randomness")
    if not eq(elgamal_decrypt(eNote, k), mul(G1, v)):
        raise DeliveryRefused(
            "k does not open eNote to the stated face: addressed to another mailbox, or corrupted")


def open_a1(delivery: Dict[str, Any], k: int, m_issuer: int) -> OpenedA1:
    """Open an A1 delivery with the mailbox secret ``k``.

    Args:
        delivery: As :func:`deliver_a1` produced it.
        k: The recipient's receiving secret.
        m_issuer: The public issuer's identity scalar, from its public record.  ``idHash`` commits
            it, so a delivery claiming a different issuer recomputes to a different commitment.

    Raises:
        DeliveryRefused: naming what does not hold.
    """
    if int(delivery.get("flavor", -1)) != FLAVOR_A1:
        raise DeliveryRefused("not an A1 delivery")
    eNote                       = _ct_of(delivery["eNote"])
    eRec                        = _ct_of(delivery["eRec"])
    S                           = mailbox_shared_recipient(k, eNote.R)
    u                           = _unwrap(delivery, S, "rho", "v", "rNote")
    _check_value(eNote, k, u["v"], u["rNote"])
    idh                         = id_hash_a1(eNote, m_issuer)
    opening                     = NoteOpening(FLAVOR_A1, u["v"], u["rho"], idh, int(delivery["predicate"]))
    return OpenedA1(opening=opening, cm=note_commitment(opening), eNote=eNote, eRec=eRec,
                    r_note=u["rNote"])


def open_a2(delivery: Dict[str, Any], k: int) -> OpenedA2:
    """Open an A2 delivery with the mailbox secret ``k``.

    Checks that the unwrapped ``r'`` is ``eIss``'s randomness, and that ``T == k*eIss.R +
    gamma*H`` -- the fold's note tie and its key tie, so a wrong ``r'`` or ``gamma`` would
    otherwise surface only as a proof that will not build.  A ``T`` keyed to some other point is
    what a minter framing a sock puppet would have to send, and it is refused here.

    Raises:
        DeliveryRefused: naming what does not hold.
    """
    if int(delivery.get("flavor", -1)) != FLAVOR_A2:
        raise DeliveryRefused("not an A2 delivery")
    eNote                       = _ct_of(delivery["eNote"])
    eIss                        = _ct_of(delivery["eIss"])
    T                           = _pt_of(delivery["T"])
    S                           = mailbox_shared_recipient(k, eNote.R)
    u                           = _unwrap(delivery, S, "rho", "v", "rPrime", "saltIss", "gamma")
    _check_value(eNote, k, u["v"], None)
    if not eq(eIss.R, mul(G1, u["rPrime"])):
        raise DeliveryRefused("the unwrapped r' is not eIss's randomness")
    if not eq(T, add(mul(eIss.R, k), mul(H_PEDERSEN, u["gamma"]))):
        raise DeliveryRefused("T does not open to this mailbox under the unwrapped gamma")
    idh                         = id_hash_a2(eNote, eIss, T)
    opening                     = NoteOpening(FLAVOR_A2, u["v"], u["rho"], idh, int(delivery["predicate"]))
    return OpenedA2(opening=opening, cm=note_commitment(opening), eNote=eNote, eIss=eIss,
                    M_I=elgamal_decrypt(eIss, k), r_prime=u["rPrime"], salt_iss=u["saltIss"],
                    T=T, gamma=u["gamma"])
