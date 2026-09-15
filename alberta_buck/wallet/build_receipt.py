"""Build a :class:`ReceiptCore` for a BUCK payment.

Reference: alberta-buck-receipt.org ("Per-Scenario Payloads").

The EOA builders are the *payee's* operation (the recipient assembles the
receipt, generating proofs with their account secret).  The Note builders are
*either* party's operation: the Identity-M architecture gives the issuer and
the recipient the same note payload — the idHash preimage material
(``eNote``/``eRec``/``eIss``/``sigma``) created at mint and delivered with the
note, plus the spend-side ``SpentCoupled*`` event data — and the receipt
discloses both identity preimages, from which any verifier derives the
identity scalars ``m_iss``/``m_rec`` and re-checks the note-leg ciphertexts
directly.  ``role`` selects the generating side:

* ``role="recipient"`` — the payee self-names via verifiable decryption of
  their own registered credential (``payee_vd``), exactly as the EOA kinds.
* ``role="issuer"`` — the payer-side "I paid X" slip.  A private (A2) issuer
  self-names via ``payer_vd``; a public (B1/A1) issuer needs no proof (their
  Identity is the public record + the batch Schnorr).  For B1 — where the
  bearer is unknown until spend — the issuer names the depositor by verifiably
  decrypting the ``SpentCoupledB1`` event's ``eDepForIss`` (``vd_payee``).

All three flavors use the unified spend nullifier ``Poseidon3(rho, idHash,
4242)`` (the shipped spend.circom derives the 4242 tag for every flavor) and
anchor to the ``SpentCoupledB1`` / ``SpentCoupledA1`` / ``SpentCoupledA2``
events.

The resulting :class:`ReceiptCore` is self-contained: serializing it and
re-verifying via :func:`alberta_buck.wallet.verify_receipt.verify_receipt`
reproduces the same named identities and amount with no secret needed.
"""

from __future__ import annotations

import json
import random
from typing import List, Optional, Tuple

from alberta_buck.wallet.bn254 import rand_scalar, scalar_to_hex
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.chaum_pedersen import CPProof
from alberta_buck.wallet.verifiable_decrypt import VDProof, verifiable_decrypt_prove
from alberta_buck.wallet.schnorr import SchnorrProof
from alberta_buck.wallet.notes import NoteOpening
from alberta_buck.wallet.envelope import (
    PartyRecord, TxnRecord, ReceiptCore,
    _g1_hex, _ct_hex,
    deserialize_core,
    vd_proof_record, cp_proof_record,
    receipts_proof_record, issuer_reenc_record, note_payload_record,
)
from alberta_buck.wallet._kernel import kernel_wallet as _kernel_wallet


def _rng(seed: int = 0):
    r = random.Random(seed)
    return lambda: r.getrandbits(256)


def _replay(vals):
    """Hand back exactly `vals` -- feeds pre-drawn nonces to the py path
    so kernel dispatch and the reference consume the caller's rng
    identically (the nonces are hoisted BEFORE branching)."""
    it = iter(vals)
    return lambda: next(it)


# ---------------------------------------------------------------------------
# Kernel dispatch: named-args JSON for buck_core.buck_wallet.build_receipt
# (the canonical-bytes ABI; see buck-wallet's `args` module).  The Python
# builders below remain the executable spec -- nonces are drawn in their
# exact order before branching, so both paths emit identical bytes.
# ---------------------------------------------------------------------------

def _args_party(addr: int, identity: str, M, pk,
                E: Optional[ElGamalCiphertext] = None,
                sk: Optional[int] = None) -> dict:
    d = {"addr": scalar_to_hex(addr), "identity": identity,
         "M": _g1_hex(M), "pk": _g1_hex(pk)}
    if E is not None:
        d["E"] = _ct_hex(E)
    if sk is not None:
        d["sk"] = scalar_to_hex(sk)
    return d


def _args_opening(o: NoteOpening) -> dict:
    return {"flavor": scalar_to_hex(o.flavor), "v": scalar_to_hex(o.v),
            "rho": scalar_to_hex(o.rho), "idHash": scalar_to_hex(o.id_hash),
            "predicate": scalar_to_hex(o.predicate)}


def _args_schnorr(s: SchnorrProof) -> dict:
    return {"e": scalar_to_hex(s.e), "s": scalar_to_hex(s.s), "R": _g1_hex(s.R)}


def _args_cp(p: CPProof) -> dict:
    return {"e": scalar_to_hex(p.e), "s1": scalar_to_hex(p.s1),
            "s2": scalar_to_hex(p.s2), "T1": _g1_hex(p.T1),
            "T2": _g1_hex(p.T2), "T3": _g1_hex(p.T3)}


def _kernel_build(kernel, args: dict) -> ReceiptCore:
    return deserialize_core(kernel.build_receipt(json.dumps(args)).encode("utf-8"))


# ---------------------------------------------------------------------------
# Payload builders (per kind)
# ---------------------------------------------------------------------------

def _party(addr: int, kind: str, identity: str, M, pk,
           E_addr: Optional[ElGamalCiphertext] = None) -> PartyRecord:
    return PartyRecord(
        addr=scalar_to_hex(addr), kind=kind, identity=identity,
        M=_g1_hex(M), pk=_g1_hex(pk),
        E_addr=_ct_hex(E_addr) if E_addr is not None else None,
    )


def _self_vd(E_own: ElGamalCiphertext, sk_own: int, M_own,
             own_addr: int, chainid: int, rng) -> dict:
    """Self-naming — prove one's own registered E_addr decrypts to one's M."""
    vd = verifiable_decrypt_prove(E_own, sk_own, M_own, own_addr, chainid, rng=rng)
    return vd_proof_record(E_own, M_own, own_addr, chainid, vd)


def _check_role(role: str) -> None:
    if role not in ("recipient", "issuer"):
        raise ValueError(f"unknown receipt role: {role}")


# ---- eoa-pub ----------------------------------------------------------------

def build_eoa_pub(
    chainid: int,
    contracts: dict,
    # Payer (public EOA — named by registry read + off-chain attestation)
    payer_addr: int, payer_identity: str, payer_M, payer_pk,
    # Payee (wallet owner)
    payee_addr: int, payee_identity: str, payee_M, payee_pk,
    payee_sk: int, payee_E_addr: ElGamalCiphertext,
    # Transaction anchor
    value: int, block_time: int,
    txhash: str, block: int, logindex: int,
    # Optional
    notes: Optional[List[str]] = None,
    rng=None,
) -> ReceiptCore:
    """Build an eoa-pub receipt (EOA transfer from a public counterparty).

    The payer is named by their public ``identity_data`` preimage recomputing
    to ``payer_M`` — no payer proof needed.  The payee self-names via
    verifiable decryption.
    """
    rng = rng or _rng()
    t_self = rand_scalar(rng)
    k = _kernel_wallet()
    if k is not None:
        return _kernel_build(k, {
            "kind": "eoa-pub", "role": "recipient",
            "chainid": chainid, "contracts": contracts,
            "payer": _args_party(payer_addr, payer_identity, payer_M, payer_pk),
            "payee": _args_party(payee_addr, payee_identity, payee_M, payee_pk,
                                 E=payee_E_addr, sk=payee_sk),
            "txn": {"value": value, "block_time": block_time, "txhash": txhash,
                    "block": block, "logindex": logindex},
            "notes": notes,
            "nonces": {"t_self": scalar_to_hex(t_self)},
        })
    rng = _replay([t_self])
    vd_self_rec = _self_vd(payee_E_addr, payee_sk, payee_M, payee_addr, chainid, rng)

    return ReceiptCore(
        v=1, type="eoa-pub", chainid=chainid, contracts=contracts,
        payer=_party(payer_addr, "public", payer_identity, payer_M, payer_pk),
        payee=_party(payee_addr, "private", payee_identity, payee_M, payee_pk,
                     E_addr=payee_E_addr),
        txn=TxnRecord(
            kind="eoa-transfer", value=value, timestamp=block_time,
            event="Transfer", txhash=txhash, block=block, logindex=logindex,
        ),
        payee_vd=vd_self_rec,
        notes=notes,
    )


# ---- eoa-priv ---------------------------------------------------------------

def build_eoa_priv(
    chainid: int,
    contracts: dict,
    # Payer (private EOA, approved the payee)
    payer_addr: int, payer_identity: str, payer_M, payer_pk,
    payer_E_addr: ElGamalCiphertext,
    E_for_payee: ElGamalCiphertext,   # payer's re-encryption for payee
    cp_proof: CPProof,                 # approve handshake (verifyApprove)
    # Payee (wallet owner)
    payee_addr: int, payee_identity: str, payee_M, payee_pk,
    payee_sk: int, payee_E_addr: ElGamalCiphertext,
    # Transaction anchor
    value: int, block_time: int,
    txhash: str, block: int, logindex: int,
    # Optional
    notes: Optional[List[str]] = None,
    rng=None,
) -> ReceiptCore:
    """Build an eoa-priv receipt (EOA transfer from a private counterparty).

    Payer naming: the approve handshake (cp_proof = verifyApprove) proves
    E_for_payee re-encrypts the payer's *registered* Identity; the payee
    verifiably decrypts it to ``payer_M``.
    """
    rng = rng or _rng()
    t_vd_payer = rand_scalar(rng)
    t_self = rand_scalar(rng)
    k = _kernel_wallet()
    if k is not None:
        return _kernel_build(k, {
            "kind": "eoa-priv", "role": "recipient",
            "chainid": chainid, "contracts": contracts,
            "payer": _args_party(payer_addr, payer_identity, payer_M, payer_pk,
                                 E=payer_E_addr),
            "payee": _args_party(payee_addr, payee_identity, payee_M, payee_pk,
                                 E=payee_E_addr, sk=payee_sk),
            "E_for_payee": _ct_hex(E_for_payee),
            "cp_proof": _args_cp(cp_proof),
            "txn": {"value": value, "block_time": block_time, "txhash": txhash,
                    "block": block, "logindex": logindex},
            "notes": notes,
            "nonces": {"t_vd_payer": scalar_to_hex(t_vd_payer),
                       "t_self": scalar_to_hex(t_self)},
        })
    rng = _replay([t_vd_payer, t_self])

    # Approve receipt: cp_proof (soundness) + vd (reveals M)
    vd_payer = verifiable_decrypt_prove(E_for_payee, payee_sk, payer_M,
                                        payee_addr, chainid, rng=rng)
    vd_payer_rec = vd_proof_record(E_for_payee, payer_M, payee_addr, chainid, vd_payer)
    ap_rec = cp_proof_record(
        payer_E_addr, E_for_payee, payer_pk, payee_pk,
        payer_addr, payee_addr, chainid, cp_proof,
    )

    vd_self_rec = _self_vd(payee_E_addr, payee_sk, payee_M, payee_addr, chainid, rng)

    return ReceiptCore(
        v=1, type="eoa-priv", chainid=chainid, contracts=contracts,
        payer=_party(payer_addr, "private", payer_identity, payer_M, payer_pk,
                     E_addr=payer_E_addr),
        payee=_party(payee_addr, "private", payee_identity, payee_M, payee_pk,
                     E_addr=payee_E_addr),
        txn=TxnRecord(
            kind="eoa-transfer", value=value, timestamp=block_time,
            event="Transfer", txhash=txhash, block=block, logindex=logindex,
        ),
        proof={"approve": ap_rec, "vd_payer": vd_payer_rec},
        payee_vd=vd_self_rec,
        notes=notes,
    )


# ---- note-b1 ----------------------------------------------------------------

def build_note_b1(
    chainid: int,
    contracts: dict,
    # Issuer (public — named by registry + batch Schnorr + idHash preimage)
    issuer_addr: int, issuer_identity: str, issuer_M, issuer_pk,
    # Payee (the depositor who cashed the note)
    payee_addr: int, payee_identity: str, payee_M, payee_pk,
    # Note data: the opening, mint batch, batch Schnorr, and the Identity-M
    # idHash preimage tail (sigma_R, sigma_s of id_hash_b1)
    opening: NoteOpening, cms, issuer_sig: SchnorrProof,
    sigma_R, sigma_s: int,
    # Transaction anchor (SpentCoupledB1; unified 4242 nullifier)
    nullifier: int, face: int,
    value: int, block_time: int,
    txhash: str, block: int, logindex: int,
    mint_txhash: str, mint_block: int,
    # Generating side
    role: str = "recipient",
    # role="recipient": the payee's self-naming secret + registered credential
    payee_sk: Optional[int] = None, payee_E_addr: Optional[ElGamalCiphertext] = None,
    # The SpentCoupledB1 event's eDepForIss (the depositor's Identity under the
    # public issuer's registered key).  Required for role="issuer" (with
    # issuer_sk, to name the depositor); optional context for role="recipient".
    eDepForIss: Optional[ElGamalCiphertext] = None,
    issuer_sk: Optional[int] = None,
    # Optional
    notes: Optional[List[str]] = None,
    rng=None,
) -> ReceiptCore:
    """Build a note-b1 receipt (bearer note from a public issuer), from either
    party's side.

    Issuer naming (both roles, deterministic): ``opening.id_hash`` recomputes
    as ``id_hash_b1(m_iss, sigma_R, sigma_s)`` with ``m_iss`` derived from the
    disclosed issuer identity preimage — the issuer is bound INTO the leaf —
    and the batch Schnorr binds the registered issuer key over keccak(cms).

    Depositor naming: role="recipient" — the payee self-names (``payee_vd``);
    role="issuer" — the issuer verifiably decrypts the event's ``eDepForIss``
    to the depositor's Identity (``vd_payee``).
    """
    _check_role(role)
    rng = rng or _rng()
    t_vd = rand_scalar(rng)
    k = _kernel_wallet()
    if k is not None:
        return _kernel_build(k, {
            "kind": "note-b1", "role": role,
            "chainid": chainid, "contracts": contracts,
            "payer": _args_party(issuer_addr, issuer_identity, issuer_M,
                                 issuer_pk, sk=issuer_sk),
            "payee": _args_party(payee_addr, payee_identity, payee_M, payee_pk,
                                 E=payee_E_addr, sk=payee_sk),
            "opening": _args_opening(opening),
            "cms": [scalar_to_hex(c) for c in cms],
            "issuer_sig": _args_schnorr(issuer_sig),
            "sigma_R": _g1_hex(sigma_R), "sigma_s": scalar_to_hex(sigma_s),
            "eDepForIss": _ct_hex(eDepForIss) if eDepForIss is not None else None,
            "nullifier": scalar_to_hex(nullifier), "face": scalar_to_hex(face),
            "txn": {"value": value, "block_time": block_time, "txhash": txhash,
                    "block": block, "logindex": logindex,
                    "mint_txhash": mint_txhash, "mint_block": mint_block},
            "notes": notes,
            "nonces": {"t_vd": scalar_to_hex(t_vd)},
        })
    rng = _replay([t_vd])

    rec_proof = receipts_proof_record(opening, cms, issuer_sig, nullifier, face)
    payload = note_payload_record(sigma_R=sigma_R, sigma_s=sigma_s,
                                  eDepForIss=eDepForIss)

    payee_vd_rec = None
    vd_payee_rec = None
    if role == "recipient":
        if payee_sk is None or payee_E_addr is None:
            raise ValueError("note-b1 recipient receipt needs payee_sk + payee_E_addr")
        payee_vd_rec = _self_vd(payee_E_addr, payee_sk, payee_M, payee_addr, chainid, rng)
    else:
        if eDepForIss is None or issuer_sk is None:
            raise ValueError("note-b1 issuer receipt needs eDepForIss + issuer_sk")
        vd = verifiable_decrypt_prove(eDepForIss, issuer_sk, payee_M,
                                      issuer_addr, chainid, rng=rng)
        vd_payee_rec = vd_proof_record(eDepForIss, payee_M, issuer_addr, chainid, vd)

    return ReceiptCore(
        v=1, type="note-b1", chainid=chainid, contracts=contracts,
        payer=_party(issuer_addr, "public", issuer_identity, issuer_M, issuer_pk),
        payee=_party(payee_addr, "private", payee_identity, payee_M, payee_pk,
                     E_addr=payee_E_addr),
        txn=TxnRecord(
            kind="note-spend", value=value, timestamp=block_time,
            event="SpentCoupledB1", txhash=txhash, block=block, logindex=logindex,
            mint_txhash=mint_txhash, mint_block=mint_block,
            nullifier=scalar_to_hex(nullifier),
        ),
        role=role,
        note=payload,
        proof=rec_proof,
        payee_vd=payee_vd_rec,
        vd_payee=vd_payee_rec,
        notes=notes,
    )


# ---- note-a1 ----------------------------------------------------------------

def build_note_a1(
    chainid: int,
    contracts: dict,
    # Issuer (public)
    issuer_addr: int, issuer_identity: str, issuer_M, issuer_pk,
    # Payee (the addressed recipient identity M_rec)
    payee_addr: int, payee_identity: str, payee_M, payee_pk,
    # Note data: the opening, mint batch, batch Schnorr, and the Identity-M
    # idHash preimage (eNote + sigma) plus the spend-side eRec
    opening: NoteOpening, cms, issuer_sig: SchnorrProof,
    eNote: ElGamalCiphertext, eRec: ElGamalCiphertext,
    sigma_R, sigma_s: int,
    # Transaction anchor (SpentCoupledA1; unified 4242 nullifier)
    nullifier: int, face: int,
    value: int, block_time: int,
    txhash: str, block: int, logindex: int,
    mint_txhash: str, mint_block: int,
    # Generating side
    role: str = "recipient",
    payee_sk: Optional[int] = None, payee_E_addr: Optional[ElGamalCiphertext] = None,
    # Optional
    notes: Optional[List[str]] = None,
    rng=None,
) -> ReceiptCore:
    """Build a note-a1 receipt (addressed note from a public issuer), from
    either party's side.

    Both namings are deterministic from the payload + disclosed preimages:
    ``opening.id_hash`` recomputes as ``id_hash_a1(eNote, m_iss, sigma_R,
    sigma_s)`` (issuer bound into the leaf, batch Schnorr over keccak(cms));
    ``eNote`` decrypts under the derivable ``m_rec`` to ``v*G`` and ``eRec``
    to ``M_rec`` (recipient bound into the leaf).  An issuer-generated A1
    receipt therefore needs no party proof at all; a recipient-generated one
    adds the payee's self-naming ``payee_vd`` (the account ↔ Identity tie).
    """
    _check_role(role)
    rng = rng or _rng()
    t_vd = rand_scalar(rng) if role == "recipient" else None
    k = _kernel_wallet()
    if k is not None:
        return _kernel_build(k, {
            "kind": "note-a1", "role": role,
            "chainid": chainid, "contracts": contracts,
            "payer": _args_party(issuer_addr, issuer_identity, issuer_M, issuer_pk),
            "payee": _args_party(payee_addr, payee_identity, payee_M, payee_pk,
                                 E=payee_E_addr, sk=payee_sk),
            "opening": _args_opening(opening),
            "cms": [scalar_to_hex(c) for c in cms],
            "issuer_sig": _args_schnorr(issuer_sig),
            "eNote": _ct_hex(eNote), "eRec": _ct_hex(eRec),
            "sigma_R": _g1_hex(sigma_R), "sigma_s": scalar_to_hex(sigma_s),
            "nullifier": scalar_to_hex(nullifier), "face": scalar_to_hex(face),
            "txn": {"value": value, "block_time": block_time, "txhash": txhash,
                    "block": block, "logindex": logindex,
                    "mint_txhash": mint_txhash, "mint_block": mint_block},
            "notes": notes,
            "nonces": ({"t_vd": scalar_to_hex(t_vd)} if t_vd is not None else {}),
        })
    rng = _replay([t_vd] if t_vd is not None else [])

    rec_proof = receipts_proof_record(opening, cms, issuer_sig, nullifier, face)
    payload = note_payload_record(eNote=eNote, eRec=eRec,
                                  sigma_R=sigma_R, sigma_s=sigma_s)

    payee_vd_rec = None
    if role == "recipient":
        if payee_sk is None or payee_E_addr is None:
            raise ValueError("note-a1 recipient receipt needs payee_sk + payee_E_addr")
        payee_vd_rec = _self_vd(payee_E_addr, payee_sk, payee_M, payee_addr, chainid, rng)

    return ReceiptCore(
        v=1, type="note-a1", chainid=chainid, contracts=contracts,
        payer=_party(issuer_addr, "public", issuer_identity, issuer_M, issuer_pk),
        payee=_party(payee_addr, "private", payee_identity, payee_M, payee_pk,
                     E_addr=payee_E_addr),
        txn=TxnRecord(
            kind="note-spend", value=value, timestamp=block_time,
            event="SpentCoupledA1", txhash=txhash, block=block, logindex=logindex,
            mint_txhash=mint_txhash, mint_block=mint_block,
            nullifier=scalar_to_hex(nullifier),
        ),
        role=role,
        note=payload,
        proof=rec_proof,
        payee_vd=payee_vd_rec,
        notes=notes,
    )


# ---- note-a2 ----------------------------------------------------------------

def build_note_a2(
    chainid: int,
    contracts: dict,
    # Issuer (private — recovered by decrypting eIss under m_rec; bound to the
    # registered credential by the mint's issuer_reenc binding)
    issuer_addr: int, issuer_identity: str, issuer_M, issuer_pk,
    issuer_E_addr: ElGamalCiphertext,
    # Payee (the addressed recipient identity M_rec)
    payee_addr: int, payee_identity: str, payee_M, payee_pk,
    # Note data: the opening + mint batch and the Identity-M idHash preimage
    opening: NoteOpening, cms,
    eNote: ElGamalCiphertext, eIss: ElGamalCiphertext,
    # Transaction anchor (SpentCoupledA2; unified 4242 nullifier)
    nullifier: int, face: int,
    value: int, block_time: int,
    txhash: str, block: int, logindex: int,
    mint_txhash: str, mint_block: int,
    # Issuer binding (shipped with the note / verified at mint): the blinded A2
    # re-encryption proof.  When present the receipt is soundly bound; when
    # None it falls back to UNVERIFIED ISSUER.
    binding=None,
    # Generating side
    role: str = "recipient",
    payee_sk: Optional[int] = None, payee_E_addr: Optional[ElGamalCiphertext] = None,
    issuer_sk: Optional[int] = None,
    # Optional
    notes: Optional[List[str]] = None,
    rng=None,
) -> ReceiptCore:
    """Build a note-a2 receipt (addressed note from a *private* issuer), from
    either party's side.

    The note legs are deterministic from the payload + disclosed preimages:
    ``opening.id_hash`` recomputes as ``id_hash_a2(eNote, eIss)``; ``eNote``
    decrypts under the derivable ``m_rec`` to ``v*G`` and ``eIss`` to the
    named issuer ``M_iss`` (which forces eIss's key to be M_rec — the
    coupling); the ``binding`` proves ``eIss`` re-encrypts the issuer's
    *registered* credential (anti-framing), so the named issuer is the true
    minter.  Self-naming: role="recipient" — ``payee_vd``; role="issuer" —
    ``payer_vd`` (the private issuer's own registered credential).
    """
    _check_role(role)
    rng = rng or _rng()
    t_vd = rand_scalar(rng)
    k = _kernel_wallet()
    if k is not None:
        return _kernel_build(k, {
            "kind": "note-a2", "role": role,
            "chainid": chainid, "contracts": contracts,
            "payer": _args_party(issuer_addr, issuer_identity, issuer_M,
                                 issuer_pk, E=issuer_E_addr, sk=issuer_sk),
            "payee": _args_party(payee_addr, payee_identity, payee_M, payee_pk,
                                 E=payee_E_addr, sk=payee_sk),
            "opening": _args_opening(opening),
            "cms": [scalar_to_hex(c) for c in cms],
            "eNote": _ct_hex(eNote), "eIss": _ct_hex(eIss),
            "binding": issuer_reenc_record(binding) if binding is not None else None,
            "nullifier": scalar_to_hex(nullifier), "face": scalar_to_hex(face),
            "txn": {"value": value, "block_time": block_time, "txhash": txhash,
                    "block": block, "logindex": logindex,
                    "mint_txhash": mint_txhash, "mint_block": mint_block},
            "notes": notes,
            "nonces": {"t_vd": scalar_to_hex(t_vd)},
        })
    rng = _replay([t_vd])

    rec_proof = receipts_proof_record(opening, cms, None, nullifier, face)
    payload = note_payload_record(eNote=eNote, eIss=eIss)

    payee_vd_rec = None
    payer_vd_rec = None
    if role == "recipient":
        if payee_sk is None or payee_E_addr is None:
            raise ValueError("note-a2 recipient receipt needs payee_sk + payee_E_addr")
        payee_vd_rec = _self_vd(payee_E_addr, payee_sk, payee_M, payee_addr, chainid, rng)
    else:
        if issuer_sk is None:
            raise ValueError("note-a2 issuer receipt needs issuer_sk")
        payer_vd_rec = _self_vd(issuer_E_addr, issuer_sk, issuer_M, issuer_addr, chainid, rng)

    bound = binding is not None
    return ReceiptCore(
        v=1, type="note-a2", chainid=chainid, contracts=contracts,
        payer=_party(issuer_addr, "private", issuer_identity, issuer_M, issuer_pk,
                     E_addr=issuer_E_addr),
        payee=_party(payee_addr, "private", payee_identity, payee_M, payee_pk,
                     E_addr=payee_E_addr),
        txn=TxnRecord(
            kind="note-spend", value=value, timestamp=block_time,
            event="SpentCoupledA2", txhash=txhash, block=block, logindex=logindex,
            mint_txhash=mint_txhash, mint_block=mint_block,
            nullifier=scalar_to_hex(nullifier),
        ),
        role=role,
        note=payload,
        proof=rec_proof,
        payee_vd=payee_vd_rec,
        payer_vd=payer_vd_rec,
        issuer_binding_status="bound" if bound else "unverified",
        issuer_binding=issuer_reenc_record(binding) if bound else None,
        notes=notes,
    )


__all__ = [
    "build_eoa_pub", "build_eoa_priv",
    "build_note_b1", "build_note_a1", "build_note_a2",
]
