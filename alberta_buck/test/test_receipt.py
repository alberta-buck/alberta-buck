"""RcptVerify tests -- the non-deniable-receipt verifier (decryptability Phase 1).

Exercises the public-issuer (B1) receipt end-to-end against the canonical
``receipt`` vector: completeness (an honest receipt verifies and names the
issuer's registered Identity), third-party re-checkability (no secret needed),
and soundness/non-frameability (tampered opening, wrong/non-public issuer,
forged batch binding, mismatched nullifier or face all reject), plus the A2
private-issuer refusal that awaits Phase 2.

See alberta-buck-notes.org ("The Non-Deniable-Receipt Invariant") and alberta-buck-receipt.org (receipts as verifiable proofs / AB-RCPT/2 "proof chain").
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from alberta_buck.wallet.bn254 import ORDER, words_to_point
from alberta_buck.wallet.notes import NoteOpening, FLAVOR_A2
from alberta_buck.wallet.schnorr import SchnorrProof
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.chaum_pedersen import CPProof
from alberta_buck.wallet.verifiable_decrypt import VDProof
from alberta_buck.wallet.receipt import (
    Receipt,
    RegisteredIdentity,
    receipt_verify,
    ApproveReceipt,
    approve_receipt_verify,
)
from alberta_buck.wallet.vectors import build_vectors


def _h(s: str) -> int:
    return int(s, 16)


def _pt(d):
    return words_to_point(_h(d["x"]), _h(d["y"]))


def _ct(d):
    return ElGamalCiphertext(R=_pt(d["R"]), C=_pt(d["C"]))


@pytest.fixture(scope="module")
def vectors():
    return build_vectors()


def _receipt(vectors) -> Receipt:
    """Reconstruct the Receipt from the emitted JSON-shaped vector (parses the
    same hex encoding the Solidity / off-chain readers consume)."""
    r = vectors["receipt"]
    o = r["opening"]
    sig = r["issuer_sig"]
    return Receipt(
        opening=NoteOpening(
            flavor=_h(o["flavor"]),
            v=_h(o["v"]),
            rho=_h(o["rho"]),
            id_hash=_h(o["idHash"]),
            predicate=_h(o["predicate"]),
        ),
        cms=tuple(_h(c) for c in r["cms"]),
        issuer=_h(r["issuer"]),
        issuer_sig=SchnorrProof(e=_h(sig["e"]), s=_h(sig["s"]), R=_pt(sig["R"])),
        chainid=_h(r["chainid"]),
        nullifier=_h(r["nullifier"]),
        face=_h(r["face"]),
        recipient=_h(r["recipient"]),
    )


def _registry(vectors, *, is_public=True, pk=None, M=None):
    """A registry view containing only the issuer's public record."""
    r = vectors["receipt"]
    return {
        _h(r["issuer"]): RegisteredIdentity(
            is_public=is_public,
            pk=pk if pk is not None else _pt(r["issuer_pk"]),
            M=M  if M  is not None else _pt(r["issuer_M"]),
        )
    }


# ---- completeness ----------------------------------------------------------

def test_receipt_verifies_and_names_issuer(vectors):
    res = receipt_verify(_receipt(vectors), _registry(vectors))
    assert res.ok, res.reason
    # The named payer is Bob's registered Identity point, and the value is the
    # note face -- exactly the (M_P, v) the invariant promises.
    assert res.identity_M == _pt(vectors["bob"]["M"])
    assert res.value == _h(vectors["receipt"]["face"])


def test_third_party_recheck_needs_no_secret(vectors):
    # A third party holds only the receipt (public) plus the registry's public
    # (pk, M) -- no secret key anywhere -- and reaches the same conclusion.
    receipt, registry = _receipt(vectors), _registry(vectors)
    res = receipt_verify(receipt, registry)
    assert res.ok
    assert res.identity_M == registry[receipt.issuer].M


# ---- soundness / non-frameability ------------------------------------------

def test_tampered_opening_rejected(vectors):
    # Bump the value: cm no longer matches any leaf in the minted batch.
    receipt = _receipt(vectors)
    bad = Receipt(
        opening=NoteOpening(
            flavor=receipt.opening.flavor,
            v=receipt.opening.v + 1,
            rho=receipt.opening.rho,
            id_hash=receipt.opening.id_hash,
            predicate=receipt.opening.predicate,
        ),
        cms=receipt.cms, issuer=receipt.issuer, issuer_sig=receipt.issuer_sig,
        chainid=receipt.chainid, nullifier=receipt.nullifier,
        face=receipt.face, recipient=receipt.recipient,
    )
    res = receipt_verify(bad, _registry(vectors))
    assert not res.ok and res.reason.startswith("(a)")


def test_unregistered_issuer_rejected(vectors):
    res = receipt_verify(_receipt(vectors), {})   # empty registry
    assert not res.ok and "not registered" in res.reason


def test_non_public_issuer_rejected(vectors):
    res = receipt_verify(_receipt(vectors), _registry(vectors, is_public=False))
    assert not res.ok and "not a public Identity" in res.reason


def test_frame_wrong_key_rejected(vectors):
    # Registry returns Alice's key for the issuer slot: the batch Schnorr was
    # signed by Bob, so it fails -- a frame attempt naming the wrong account.
    wrong_pk = _pt(vectors["alice"]["elgamal_kp"]["pk"])
    res = receipt_verify(_receipt(vectors), _registry(vectors, pk=wrong_pk))
    assert not res.ok and res.reason.startswith("(b)/(c)")


def test_tampered_batch_sig_rejected(vectors):
    receipt = _receipt(vectors)
    s = receipt.issuer_sig
    bad = Receipt(
        opening=receipt.opening, cms=receipt.cms, issuer=receipt.issuer,
        issuer_sig=SchnorrProof(e=s.e, s=(s.s + 1) % ORDER, R=s.R),
        chainid=receipt.chainid, nullifier=receipt.nullifier,
        face=receipt.face, recipient=receipt.recipient,
    )
    res = receipt_verify(bad, _registry(vectors))
    assert not res.ok and res.reason.startswith("(b)/(c)")


def test_wrong_nullifier_rejected(vectors):
    receipt = _receipt(vectors)
    bad = Receipt(
        opening=receipt.opening, cms=receipt.cms, issuer=receipt.issuer,
        issuer_sig=receipt.issuer_sig, chainid=receipt.chainid,
        nullifier=receipt.nullifier ^ 1, face=receipt.face,
        recipient=receipt.recipient,
    )
    res = receipt_verify(bad, _registry(vectors))
    assert not res.ok and res.reason.startswith("(d)")


def test_wrong_face_rejected(vectors):
    receipt = _receipt(vectors)
    bad = Receipt(
        opening=receipt.opening, cms=receipt.cms, issuer=receipt.issuer,
        issuer_sig=receipt.issuer_sig, chainid=receipt.chainid,
        nullifier=receipt.nullifier, face=receipt.face + 1,
        recipient=receipt.recipient,
    )
    res = receipt_verify(bad, _registry(vectors))
    assert not res.ok and res.reason.startswith("(d)")


# ---- A2 private issuer: refused until Phase 2 ------------------------------

def test_a2_private_issuer_refused(vectors):
    receipt = _receipt(vectors)
    a2 = Receipt(
        opening=NoteOpening(
            flavor=FLAVOR_A2,
            v=receipt.opening.v,
            rho=receipt.opening.rho,
            id_hash=receipt.opening.id_hash,
            predicate=receipt.opening.predicate,
        ),
        cms=receipt.cms, issuer=receipt.issuer, issuer_sig=receipt.issuer_sig,
        chainid=receipt.chainid, nullifier=receipt.nullifier,
        face=receipt.face, recipient=receipt.recipient,
    )
    res = receipt_verify(a2, _registry(vectors))
    assert not res.ok and "Phase 2" in res.reason


# ---- EOA approve receipt ---------------------------------------------------

def _approve_receipt(vectors) -> ApproveReceipt:
    r = vectors["approve_receipt"]
    cp, vd = r["cp_proof"], r["vd_proof"]
    return ApproveReceipt(
        sender=_h(r["sender"]),
        spender=_h(r["spender"]),
        chainid=_h(r["chainid"]),
        registry_addr=_h(r["registry"]),
        E_for_spender=_ct(r["E_for_spender"]),
        cp_proof=CPProof(
            e=_h(cp["e"]), s1=_h(cp["s1"]), s2=_h(cp["s2"]),
            T1=_pt(cp["T1"]), T2=_pt(cp["T2"]), T3=_pt(cp["T3"]),
        ),
        M_named=_pt(r["M_named"]),
        vd_proof=VDProof(e=_h(vd["e"]), s=_h(vd["s"]), T1=_pt(vd["T1"]), T2=_pt(vd["T2"])),
    )


def _approve_registry(vectors, *, sender_E=None, drop_sender=False, drop_spender=False):
    """A registry view with the sender's (pk, E_addr) and the spender's pk."""
    r = vectors["approve_receipt"]
    reg = {}
    if not drop_sender:
        reg[_h(r["sender"])] = RegisteredIdentity(
            is_public=False,
            pk=_pt(r["sender_pk"]),
            E_addr=sender_E if sender_E is not None else _ct(r["sender_E_addr"]),
        )
    if not drop_spender:
        reg[_h(r["spender"])] = RegisteredIdentity(is_public=False, pk=_pt(r["spender_pk"]))
    return reg


def test_approve_receipt_verifies_and_names_sender(vectors):
    res = approve_receipt_verify(_approve_receipt(vectors), _approve_registry(vectors))
    assert res.ok, res.reason
    assert res.identity_M == _pt(vectors["alice"]["M"])   # names Alice, the sender
    assert res.value is None                              # amount is the Transfer log


def test_approve_receipt_third_party_recheck_needs_no_secret(vectors):
    receipt, registry = _approve_receipt(vectors), _approve_registry(vectors)
    res = approve_receipt_verify(receipt, registry)
    assert res.ok
    assert res.identity_M == _pt(vectors["approve_receipt"]["M_named"])


def test_approve_receipt_frame_wrong_M_rejected(vectors):
    # Spender tries to name Bob's Identity instead of Alice's: vd_proof fails.
    bad = replace(_approve_receipt(vectors), M_named=_pt(vectors["bob"]["M"]))
    res = approve_receipt_verify(bad, _approve_registry(vectors))
    assert not res.ok and res.reason.startswith("(recovery)")


def test_approve_receipt_tampered_cp_rejected(vectors):
    receipt = _approve_receipt(vectors)
    bad = replace(receipt, cp_proof=replace(receipt.cp_proof,
                                            s1=(receipt.cp_proof.s1 + 1) % ORDER))
    res = approve_receipt_verify(bad, _approve_registry(vectors))
    assert not res.ok and res.reason.startswith("(soundness)")


def test_approve_receipt_tampered_vd_rejected(vectors):
    receipt = _approve_receipt(vectors)
    bad = replace(receipt, vd_proof=replace(receipt.vd_proof,
                                            s=(receipt.vd_proof.s + 1) % ORDER))
    res = approve_receipt_verify(bad, _approve_registry(vectors))
    assert not res.ok and res.reason.startswith("(recovery)")


def test_approve_receipt_substituted_sender_record_rejected(vectors):
    # Registry returns a different "registered" ciphertext for the sender; the
    # cp_proof no longer matches -- the receipt cannot forge the on-chain record.
    bad_E = _ct(vectors["bob"]["ciphertext"])
    res = approve_receipt_verify(_approve_receipt(vectors),
                                 _approve_registry(vectors, sender_E=bad_E))
    assert not res.ok and res.reason.startswith("(soundness)")


def test_approve_receipt_unregistered_sender_rejected(vectors):
    res = approve_receipt_verify(_approve_receipt(vectors),
                                 _approve_registry(vectors, drop_sender=True))
    assert not res.ok and "not registered" in res.reason
