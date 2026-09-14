"""Tier-1 offline receipt verification from a :class:`ReceiptCore`.

Reference: alberta-buck-receipt.org ("Verification Procedure").

Consumes a deserialized receipt core (no chain access needed for tier 1) and
re-runs every check: the point→human bridge (keccak(identity)·G == M) for each
party, the generating side's self-naming verifiable decryption, and the
type-specific naming of both parties.

For the Note kinds the naming is *deterministic* — the receipt discloses both
identity preimages, so the verifier derives the identity scalars m_iss/m_rec
itself, recomputes the Identity-M ``idHash`` from the embedded note payload
(``id_hash_b1/a1/a2``), and decrypts the payload ciphertexts directly:

* note-b1:  idHash == id_hash_b1(m_iss, sigma_R, sigma_s) — the issuer is
  bound INTO the leaf; the batch Schnorr binds the registered issuer key over
  keccak(cms).  An issuer-generated receipt additionally names the depositor
  via the verifiable decryption of the SpentCoupledB1 event's eDepForIss.
* note-a1:  idHash == id_hash_a1(eNote, m_iss, sigma_R, sigma_s);
  Dec(eNote, m_rec) == v·G and Dec(eRec, m_rec) == M_rec — both parties bound
  into the leaf (only the addressed identity satisfies the eNote relation).
* note-a2:  idHash == id_hash_a2(eNote, eIss); Dec(eNote, m_rec) == v·G;
  Dec(eIss, m_rec) == the named issuer M — which algebraically forces eIss's
  key to BE M_rec (the coupling) — and the mint's issuer_reenc binding proves
  eIss re-encrypts the issuer's *registered* credential (anti-framing).
  Without a binding the receipt still verifies but is stamped
  UNVERIFIED ISSUER.

All flavors use the unified spend nullifier ``Poseidon3(rho, idHash, 4242)``
(the shipped spend.circom tag) anchored at the ``SpentCoupled*`` event.

Tier 2 (chain anchoring) is out of scope — the verifier would read the
embedded ``pk`` / ``E_addr`` and event references from a node.
"""

from __future__ import annotations

from alberta_buck.wallet.bn254 import G1, mul, add, neg, eq
from alberta_buck.wallet.elgamal import ElGamalCiphertext, elgamal_decrypt
from alberta_buck.wallet.issuer_reenc import IssuerReencProof, issuer_reenc_verify
from alberta_buck.wallet.identity import identity_scalar
from alberta_buck.wallet.chaum_pedersen import CPProof, chaum_pedersen_verify
from alberta_buck.wallet.verifiable_decrypt import VDProof, verifiable_decrypt_verify
from alberta_buck.wallet.schnorr import SchnorrProof, batch_commitment, issuer_schnorr_verify
from alberta_buck.wallet.notes import (
    NoteOpening, note_commitment, nullifier_b,
    FLAVOR_A1, FLAVOR_A2, FLAVOR_B1,
    id_hash_a1, id_hash_a2, id_hash_b1,
)
from alberta_buck.wallet.receipt import RcptResult
from alberta_buck.wallet.envelope import (
    ReceiptCore, _ct_from_hex, _g1_from_hex,
)


def _h(s: str) -> int:
    return int(s, 16)


def _check_point_identity(M_pt, identity: str) -> bool:
    """Recompute ``M' = keccak(identity)·G`` and check equality with ``M_pt``.

    ``identity`` is a canonical JSON string (the PartyRecord.identity field).
    """
    m = identity_scalar(identity)
    M_prime = mul(G1, m)
    return eq(M_prime, M_pt)


def _vd_from_record(rec: dict) -> tuple:
    """Extract (E_ct, M, account, chainid, VDProof) from a vd proof record."""
    E_ct = _ct_from_hex(rec["E_ct"])
    M    = _g1_from_hex(rec["M_named"])
    account = _h(rec["account"])
    chainid = rec["chainid"]
    p = rec["proof"]
    vd = VDProof(e=_h(p["e"]), s=_h(p["s"]),
                 T1=_g1_from_hex(p["T1"]), T2=_g1_from_hex(p["T2"]))
    return E_ct, M, account, chainid, vd


def _ct_eq(a: ElGamalCiphertext, b: ElGamalCiphertext) -> bool:
    return eq(a.R, b.R) and eq(a.C, b.C)


def _self_naming_ok(vd_rec: dict, party, chainid: int) -> str:
    """Check a self-naming vd record against *party*'s registered record.

    Returns "" on success, else the failure reason.  The record must decrypt
    the party's own registered E_addr, under their registered key, to their
    named M, bound to their address and this chain.
    """
    E, M, acct, cid, vd = _vd_from_record(vd_rec)
    if cid != chainid:
        return "chainid mismatch"
    if acct != party.addr_int:
        return "account mismatch"
    if not eq(M, party.M_pt):
        return "named M mismatch"
    E_reg = party.E_addr_ct
    if E_reg is None or not _ct_eq(E, E_reg):
        return "ciphertext is not the registered E_addr"
    if not verifiable_decrypt_verify(E, party.pk_pt, M, vd, acct, cid):
        return "vd fails"
    return ""


def verify_receipt(core: ReceiptCore) -> RcptResult:
    """Tier-1 offline verification of an AB-RCPT/1 receipt core.

    Checks:
      1. Point→human bridge for both payer and payee.
      2. The generating side's self-naming (per ``core.role``).
      3. Type-specific naming of both parties (deterministic for Notes).
      4. The note anchor: cm ∈ cms, idHash preimage, nullifier, face.

    Tier 2 (chain anchoring) requires an RPC node and is not run here.

    Dispatches wholesale to the compiled buck-wallet kernel when built
    (one FFI call instead of dozens of primitive ones); the Python below
    remains the executable spec, proven check-for-check and
    reason-for-reason by the wallet kernel vectors.
    """
    from alberta_buck.wallet._kernel import kernel_wallet as _kw
    k = _kw()
    if k is not None:
        import json as _json
        from alberta_buck.wallet.envelope import serialize_core
        res = _json.loads(k.verify_receipt(serialize_core(core).decode("utf-8")))
        return RcptResult(
            ok=res["ok"],
            identity_M=(_g1_from_hex(res["identity_M"])
                        if res.get("identity_M") else None),
            value=(int(res["value"], 16) if res.get("value") else None),
            reason=res["reason"],
        )

    t = core.type
    role = core.role

    if role not in ("recipient", "issuer"):
        return RcptResult(False, None, None, f"unknown receipt role: {role}")
    if role == "issuer" and t not in ("note-b1", "note-a1", "note-a2"):
        return RcptResult(False, None, None, f"{t}: issuer-side receipts exist for Notes only")

    # 1. Point→human bridge
    if not _check_point_identity(core.payer.M_pt, core.payer.identity):
        return RcptResult(False, None, None, "payer M != keccak(identity)·G")
    if not _check_point_identity(core.payee.M_pt, core.payee.identity):
        return RcptResult(False, None, None, "payee M != keccak(identity)·G")

    # 2. Generator self-naming (the account ↔ Identity tie)
    if role == "recipient":
        if core.payee_vd is None:
            return RcptResult(False, None, None, "missing payee self-naming proof")
        why = _self_naming_ok(core.payee_vd, core.payee, core.chainid)
        if why:
            return RcptResult(False, None, None, f"payee self-naming: {why}")
    else:
        # issuer-side: a private payer must self-name; a public payer's
        # Identity is the public record (+ the batch Schnorr below).
        if core.payer.kind == "private":
            if core.payer_vd is None:
                return RcptResult(False, None, None, "missing payer self-naming proof")
            why = _self_naming_ok(core.payer_vd, core.payer, core.chainid)
            if why:
                return RcptResult(False, None, None, f"payer self-naming: {why}")

    # 3. Type-specific naming

    if t == "eoa-pub":
        # No payer proof — the payer's identity preimage → M is the naming.
        # Tier 2 anchors the address→registry record.
        pass

    elif t == "eoa-priv":
        if core.proof is None:
            return RcptResult(False, None, None, "eoa-priv: missing proof")
        ap = core.proof.get("approve")
        vp = core.proof.get("vd_payer")
        if ap is None or vp is None:
            return RcptResult(False, None, None, "eoa-priv: missing approve/vd_payer")
        if (
            not isinstance(ap, dict)
            or ap.get("protocol") != "AlbertaBuck:Approve:v3"
        ):
            return RcptResult(
                False, None, None,
                "eoa-priv: legacy approve proof; regenerate as AlbertaBuck:Approve:v3",
            )
        # Approve handshake (soundness)
        E_payer = _ct_from_hex(ap["E_a"])
        E_spender = _ct_from_hex(ap["E_b"])
        pk_payer = _g1_from_hex(ap["pk_a"])
        pk_spender = _g1_from_hex(ap["pk_b"])
        sender = _h(ap["sender"])
        spender = _h(ap["spender"])
        cid_ap = ap["chainid"]
        registry = _h(core.contracts["registry"])
        nonce_raw = ap.get("nonce")
        if not isinstance(nonce_raw, str):
            return RcptResult(False, None, None, "eoa-priv: invalid approve nonce")
        try:
            nonce = _h(nonce_raw)
        except ValueError:
            return RcptResult(False, None, None, "eoa-priv: invalid approve nonce")
        p = ap["proof"]
        cp = CPProof(e=_h(p["e"]), s1=_h(p["s1"]), s2=_h(p["s2"]),
                     T1=_g1_from_hex(p["T1"]), T2=_g1_from_hex(p["T2"]),
                     T3=_g1_from_hex(p["T3"]))
        if not chaum_pedersen_verify(E_payer, E_spender, pk_payer, pk_spender,
                                      cp, sender, spender, cid_ap,
                                      registry, nonce):
            return RcptResult(False, None, None, "eoa-priv: approve handshake fails")
        # Verifiable decryption (recovery + provability)
        E_vd, M_vd, acct_vd, cid_vd, vd = _vd_from_record(vp)
        if not verifiable_decrypt_verify(E_vd, core.payee.pk_pt, M_vd,
                                          vd, acct_vd, cid_vd):
            return RcptResult(False, None, None, "eoa-priv: vd_payer fails")
        if not eq(M_vd, core.payer.M_pt):
            return RcptResult(False, None, None, "eoa-priv: named M != payer M")

    elif t in ("note-b1", "note-a1", "note-a2"):
        if core.proof is None:
            return RcptResult(False, None, None, f"{t}: missing proof")
        if core.note is None:
            return RcptResult(False, None, None, f"{t}: missing Identity-M note payload")
        rp = core.proof
        np = core.note
        o = rp["opening"]
        opening = NoteOpening(
            flavor=_h(o["flavor"]), v=_h(o["v"]), rho=_h(o["rho"]),
            id_hash=_h(o["idHash"]), predicate=_h(o["predicate"]),
        )
        expect_flavor = {"note-b1": FLAVOR_B1, "note-a1": FLAVOR_A1,
                         "note-a2": FLAVOR_A2}[t]
        if opening.flavor != expect_flavor:
            return RcptResult(False, None, None, f"{t}: opening flavor mismatch")
        cms = [_h(c) for c in rp["cms"]]

        # (a) cm is in the minted batch
        cm = note_commitment(opening)
        if cm not in cms:
            return RcptResult(False, None, None, f"{t}: opening cm not in minted batch")

        # The identity scalars — derivable by ANY verifier from the disclosed
        # preimages (step 1 already tied them to the named M points).
        m_iss = identity_scalar(core.payer.identity)
        m_rec = identity_scalar(core.payee.identity)

        # (b) Identity-M idHash preimage: the named parties are bound INTO the
        #     leaf the spend SNARK consumed.
        if t == "note-b1":
            sigma_R = _g1_from_hex(np["sigma_R"])
            sigma_s = _h(np["sigma_s"])
            if id_hash_b1(m_iss, sigma_R, sigma_s) != opening.id_hash:
                return RcptResult(False, None, None,
                                  "note-b1: idHash != id_hash_b1(m_iss, sigma)")
        elif t == "note-a1":
            eNote = _ct_from_hex(np["eNote"])
            eRec  = _ct_from_hex(np["eRec"])
            sigma_R = _g1_from_hex(np["sigma_R"])
            sigma_s = _h(np["sigma_s"])
            if id_hash_a1(eNote, m_iss, sigma_R, sigma_s) != opening.id_hash:
                return RcptResult(False, None, None,
                                  "note-a1: idHash != id_hash_a1(eNote, m_iss, sigma)")
            # The addressed-recipient legs: only m_rec satisfies these.
            if not eq(elgamal_decrypt(eNote, m_rec), mul(G1, opening.v)):
                return RcptResult(False, None, None,
                                  "note-a1: eNote does not decrypt to v·G under m_rec")
            if not eq(elgamal_decrypt(eRec, m_rec), core.payee.M_pt):
                return RcptResult(False, None, None,
                                  "note-a1: eRec does not decrypt to M_rec under m_rec")
        else:  # note-a2
            eNote = _ct_from_hex(np["eNote"])
            eIss  = _ct_from_hex(np["eIss"])
            if id_hash_a2(eNote, eIss) != opening.id_hash:
                return RcptResult(False, None, None,
                                  "note-a2: idHash != id_hash_a2(eNote, eIss)")
            if not eq(elgamal_decrypt(eNote, m_rec), mul(G1, opening.v)):
                return RcptResult(False, None, None,
                                  "note-a2: eNote does not decrypt to v·G under m_rec")
            # Decrypting eIss under m_rec to the NAMED issuer M is the
            # coupling: it forces eIss's key to be M_rec.
            if not eq(elgamal_decrypt(eIss, m_rec), core.payer.M_pt):
                return RcptResult(False, None, None,
                                  "note-a2: eIss does not decrypt to issuer M under m_rec")

        # (c) Issuer binding over the batch / leaf.
        if t in ("note-b1", "note-a1"):
            sig = rp.get("issuer_sig")
            if sig is None:
                return RcptResult(False, None, None, f"{t}: missing issuer batch Schnorr")
            h_batch = batch_commitment(cms)
            iss_sig = SchnorrProof(e=_h(sig["e"]), s=_h(sig["s"]),
                                   R=_g1_from_hex(sig["R"]))
            if not issuer_schnorr_verify(core.payer.pk_pt, iss_sig, h_batch,
                                          core.payer.addr_int, core.chainid):
                return RcptResult(False, None, None, f"{t}: issuer batch binding fails")
        else:  # note-a2: the mint's per-leaf re-encryption binding (anti-framing)
            if core.issuer_binding is not None:
                b = core.issuer_binding
                binding = IssuerReencProof(
                    e=_h(b["e"]), s_r=_h(b["s_r"]), s_b=_h(b["s_b"]),
                    s_s=_h(b["s_s"]), s_g=_h(b["s_g"]),
                    A1=_g1_from_hex(b["A1"]), A2=_g1_from_hex(b["A2"]),
                    A3=_g1_from_hex(b["A3"]), A4=_g1_from_hex(b["A4"]),
                    A5=_g1_from_hex(b["A5"]),
                    Q=_g1_from_hex(b["Q"]), U=_g1_from_hex(b["U"]),
                    T=_g1_from_hex(b["T"]),
                )
                E_reg = core.payer.E_addr_ct
                if E_reg is None:
                    return RcptResult(False, None, None, "note-a2: issuer E_addr missing")
                if not issuer_reenc_verify(core.payer.pk_pt, E_reg, eIss,
                                           binding, core.payer.addr_int, core.chainid):
                    return RcptResult(False, None, None, "note-a2: issuer binding fails")
            # else: accept; UNVERIFIED ISSUER banner set below.

        # (d) Spend anchor: the unified 4242 nullifier + the paid face.
        nf = _h(rp["nullifier"])
        face = _h(rp["face"])
        if nf != nullifier_b(opening.rho, opening.id_hash):
            return RcptResult(False, None, None, f"{t}: nullifier mismatch")
        if face != opening.v:
            return RcptResult(False, None, None, f"{t}: face != note value")
        if core.txn.value != face:
            return RcptResult(False, None, None, f"{t}: txn value != face")

        # (e) Issuer-side B1: the depositor naming via the SpentCoupledB1
        #     event's eDepForIss (the only leg needing a proof — the issuer's
        #     verifiable decryption under their registered key).
        if t == "note-b1" and role == "issuer":
            eDep_hex = np.get("eDepForIss")
            if eDep_hex is None or core.vd_payee is None:
                return RcptResult(False, None, None,
                                  "note-b1: issuer receipt needs eDepForIss + vd_payee")
            eDep = _ct_from_hex(eDep_hex)
            E, M, acct, cid, vd = _vd_from_record(core.vd_payee)
            if cid != core.chainid or acct != core.payer.addr_int:
                return RcptResult(False, None, None, "note-b1: vd_payee context mismatch")
            if not _ct_eq(E, eDep):
                return RcptResult(False, None, None, "note-b1: vd_payee ciphertext != eDepForIss")
            if not eq(M, core.payee.M_pt):
                return RcptResult(False, None, None, "note-b1: vd_payee names a different M")
            if not verifiable_decrypt_verify(eDep, core.payer.pk_pt, M, vd, acct, cid):
                return RcptResult(False, None, None, "note-b1: vd_payee fails")

    else:
        return RcptResult(False, None, None, f"unknown receipt type: {t}")

    status = ("UNVERIFIED ISSUER"
              if (t == "note-a2" and core.issuer_binding is None)
              else "VALID")
    return RcptResult(True, core.payer.M_pt, core.txn.value, status)


__all__ = ["verify_receipt"]
