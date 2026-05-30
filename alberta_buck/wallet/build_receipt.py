"""Build a :class:`ReceiptCore` for a received BUCK payment.

Reference: alberta-buck-receipt.org ("Per-Scenario Payloads").

The entry point is :func:`build_receipt` — given a transaction reference and
the data the recipient's wallet holds (own keys + identity, counterparty
approve material / note openings / registry reads), it constructs the verified
core of an AB-RCPT/1 receipt.  The resulting :class:`ReceiptCore` is
self-contained: serializing it and re-verifying it via :func:`verify_receipt`
(see :mod:`alberta_buck.wallet.verify_receipt`) reproduces the same named
identities and amount with no secret needed.

The builder is the *payee's* operation: it generates proofs using the payee's
secret key (verifiable decryptions of their own credential and, for private
counterparties, the approve ciphertext).  Those proofs are embedded in the
receipt; a verifier consumes only the public material.

For the four fully-shipped kinds today (eoa-pub, eoa-priv, note-b1, note-a1):
the payer-naming is soundly bound to the on-chain registry records.  For A2
(private issuer), the issuer's identity point is recovered and a verifiable
decryption proves it — but soundness (that the recovered point is the
*registered* issuer) awaits the Notes Phase-2 in-SNARK binding, so the receipt
carries ``issuer_binding_status = "unverified"``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List, Optional, Tuple

from alberta_buck.wallet.bn254 import ORDER, scalar_to_hex
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.chaum_pedersen import CPProof
from alberta_buck.wallet.verifiable_decrypt import VDProof, verifiable_decrypt_prove
from alberta_buck.wallet.schnorr import SchnorrProof
from alberta_buck.wallet.notes import (
    NoteOpening, FLAVOR_A1, FLAVOR_A2, FLAVOR_B1,
    note_commitment, nullifier_a, nullifier_b,
)
from alberta_buck.wallet.envelope import (
    PartyRecord, TxnRecord, ReceiptCore,
    _g1_hex, _ct_hex,
    vd_proof_record, cp_proof_record,
    schnorr_proof_record, receipts_proof_record,
)


def _rng(seed: int = 0):
    r = random.Random(seed)
    return lambda: r.getrandbits(256)


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


def _payee_vd(E_payee: ElGamalCiphertext, sk_payee: int, M_payee,
              payee_addr: int, chainid: int,
              rng) -> Tuple[VDProof, dict]:
    """Payee self-naming — prove own E_addr decrypts to own M."""
    vd = verifiable_decrypt_prove(E_payee, sk_payee, M_payee, payee_addr, chainid, rng=rng)
    record = vd_proof_record(E_payee, M_payee, payee_addr, chainid, vd)
    return vd, record


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
    vd_self, vd_self_rec = _payee_vd(payee_E_addr, payee_sk, payee_M, payee_addr, chainid, rng)

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

    # Approve receipt: cp_proof (soundness) + vd (reveals M)
    vd_payer, vd_payer_rec = _payee_vd(E_for_payee, payee_sk, payer_M, payee_addr, chainid, rng)
    ap_rec = cp_proof_record(payer_E_addr, E_for_payee, payer_pk, payee_pk,
                             payer_addr, payee_addr, chainid, cp_proof)

    vd_self, vd_self_rec = _payee_vd(payee_E_addr, payee_sk, payee_M, payee_addr, chainid, rng)

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
    # Issuer (public — named by registry + batch Schnorr)
    issuer_addr: int, issuer_identity: str, issuer_M, issuer_pk,
    # Payee (wallet owner, the depositor who spent the note)
    payee_addr: int, payee_identity: str, payee_M, payee_pk,
    payee_sk: int, payee_E_addr: ElGamalCiphertext,
    # Note data
    opening: NoteOpening, cms, issuer_sig: SchnorrProof,
    # Transaction anchor
    nullifier: int, face: int,
    value: int, block_time: int,
    txhash: str, block: int, logindex: int,
    mint_txhash: str, mint_block: int,
    # Optional
    notes: Optional[List[str]] = None,
    rng=None,
) -> ReceiptCore:
    """Build a note-b1 receipt (bearer note from a public issuer).

    Payer naming: the four-link chain — opening → cm in cms[], issuer's
    batch Schnorr over keccak(cms) binds every leaf to the issuer's
    registered Identity; the nullifier was burned in the Spent/SpentB event.
    """
    rng = rng or _rng()

    rec_proof = receipts_proof_record(opening, cms, issuer_sig, nullifier, face)

    vd_self, vd_self_rec = _payee_vd(payee_E_addr, payee_sk, payee_M, payee_addr, chainid, rng)

    return ReceiptCore(
        v=1, type="note-b1", chainid=chainid, contracts=contracts,
        payer=_party(issuer_addr, "public", issuer_identity, issuer_M, issuer_pk),
        payee=_party(payee_addr, "private", payee_identity, payee_M, payee_pk,
                     E_addr=payee_E_addr),
        txn=TxnRecord(
            kind="note-spend", value=value, timestamp=block_time,
            event="Spent", txhash=txhash, block=block, logindex=logindex,
            mint_txhash=mint_txhash, mint_block=mint_block,
            nullifier=scalar_to_hex(nullifier),
        ),
        proof=rec_proof,
        payee_vd=vd_self_rec,
        notes=notes,
    )


# ---- note-a1 ----------------------------------------------------------------

def build_note_a1(
    chainid: int,
    contracts: dict,
    # Issuer (public)
    issuer_addr: int, issuer_identity: str, issuer_M, issuer_pk,
    # Payee (wallet owner — the addressed depositor; the A-spend CP-DLEQ
    # ensures only the sk owner of the mint-time pk_rec can spend)
    payee_addr: int, payee_identity: str, payee_M, payee_pk,
    payee_sk: int, payee_E_addr: ElGamalCiphertext,
    # Note data
    opening: NoteOpening, cms, issuer_sig: SchnorrProof,
    # Transaction anchor (A-spend uses SpentA, nullifier tag 4243)
    nullifier: int, face: int,
    value: int, block_time: int,
    txhash: str, block: int, logindex: int,
    mint_txhash: str, mint_block: int,
    # Optional
    notes: Optional[List[str]] = None,
    rng=None,
) -> ReceiptCore:
    """Build a note-a1 receipt (addressed note from a public issuer).

    Same four-link chain as B1, with the A-spend CP-DLEQ identity binding
    (verifySpendCP) on the depositor side already enforced on-chain; the
    nullifier uses tag 4243 (SpentA event).
    """
    rng = rng or _rng()

    rec_proof = receipts_proof_record(opening, cms, issuer_sig, nullifier, face)

    vd_self, vd_self_rec = _payee_vd(payee_E_addr, payee_sk, payee_M, payee_addr, chainid, rng)

    return ReceiptCore(
        v=1, type="note-a1", chainid=chainid, contracts=contracts,
        payer=_party(issuer_addr, "public", issuer_identity, issuer_M, issuer_pk),
        payee=_party(payee_addr, "private", payee_identity, payee_M, payee_pk,
                     E_addr=payee_E_addr),
        txn=TxnRecord(
            kind="note-spend", value=value, timestamp=block_time,
            event="SpentA", txhash=txhash, block=block, logindex=logindex,
            mint_txhash=mint_txhash, mint_block=mint_block,
            nullifier=scalar_to_hex(nullifier),
        ),
        proof=rec_proof,
        payee_vd=vd_self_rec,
        notes=notes,
    )


# ---- note-a2 ----------------------------------------------------------------

def build_note_a2(
    chainid: int,
    contracts: dict,
    # Issuer (private — recovered via E_iss_for_rec decryption; soundness
    # not yet bound on-chain, Phase 2 pending)
    issuer_addr: int, issuer_identity: str, issuer_M, issuer_pk,
    issuer_E_addr: ElGamalCiphertext,
    E_iss_for_rec: ElGamalCiphertext,
    # Payee (wallet owner — decrypts E_iss_for_rec)
    payee_addr: int, payee_identity: str, payee_M, payee_pk,
    payee_sk: int, payee_E_addr: ElGamalCiphertext,
    # Transaction anchor
    value: int, block_time: int,
    txhash: str, block: int, logindex: int,
    mint_txhash: str, mint_block: int, nullifier: int,
    # Optional
    notes: Optional[List[str]] = None,
    rng=None,
) -> ReceiptCore:
    """Build a note-a2 receipt (addressed note from a *private* issuer).

    The issuer hid behind E_iss-for-rec in the leaf's idHash; the payee
    verifiably decrypts it to recover issuer_M.  BUT soundness — that
    E_iss-for_rec re-encrypts the issuer's *registered* credential — needs
    the Notes Phase-2 in-SNARK binding, so the receipt carries
    ``issuer_binding_status = "unverified"``.

    The payee self-names via verifiable decryption of their own E_addr.
    """
    rng = rng or _rng()

    # Verifiable decrypt of E_iss-for_rec → issuer_M
    vd_iss, vd_iss_rec = _payee_vd(E_iss_for_rec, payee_sk, issuer_M, payee_addr, chainid, rng)

    vd_self, vd_self_rec = _payee_vd(payee_E_addr, payee_sk, payee_M, payee_addr, chainid, rng)

    return ReceiptCore(
        v=1, type="note-a2", chainid=chainid, contracts=contracts,
        payer=_party(issuer_addr, "private", issuer_identity, issuer_M, issuer_pk,
                     E_addr=issuer_E_addr),
        payee=_party(payee_addr, "private", payee_identity, payee_M, payee_pk,
                     E_addr=payee_E_addr),
        txn=TxnRecord(
            kind="note-spend", value=value, timestamp=block_time,
            event="SpentA", txhash=txhash, block=block, logindex=logindex,
            mint_txhash=mint_txhash, mint_block=mint_block,
            nullifier=scalar_to_hex(nullifier),
        ),
        vd_issuer=vd_iss_rec,
        payee_vd=vd_self_rec,
        issuer_binding_status="unverified",
        notes=notes,
    )


__all__ = [
    "build_eoa_pub", "build_eoa_priv",
    "build_note_b1", "build_note_a1", "build_note_a2",
]
