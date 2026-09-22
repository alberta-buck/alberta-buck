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
* note-a2:  idHash == id_hash_a2(eNote, eIss, T); the addressed legs open
  eNote to v·G and eIss to the named issuer M under pkRecv; the mint's
  issuer_reenc binding proves eIss re-encrypts the issuer's *registered*
  credential (anti-framing); and M == C_iss - T + gamma·H ties that
  credential's plaintext to the named M, so a minter cannot key eIss to open
  to a sock puppet.  Without a binding the receipt still verifies but is
  stamped UNVERIFIED ISSUER.

All flavors use the unified spend nullifier ``Poseidon3(rho, idHash, 4242)``
(the shipped spend.circom tag) anchored at the ``SpentCoupled*`` event.

Tier 2 (chain anchoring) is out of scope — the verifier would read the
embedded ``pk`` / ``E_addr`` and event references from a node.
"""

from __future__ import annotations

from alberta_buck.wallet.bn254 import G1, ORDER, mul, add, neg, eq
from alberta_buck.wallet.elgamal import (
    ElGamalCiphertext, elgamal_decrypt, elgamal_encrypt,
)
from alberta_buck.registry.tree import mailbox_leaf
from alberta_buck.wallet.nums import H_PEDERSEN
from alberta_buck.wallet.poseidon import poseidon
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


def _check_addressed_legs(t: str, np: dict, role: str, pk_recv, eNote, eId,
                          M_id, v: int, core) -> "str | None":
    """Check what the generating side proved about the note's ciphertexts.

    Returns an error string, or None when every leg holds.

    Two disjoint procedures, because the two parties hold different things and
    neither can produce the other's evidence.  That asymmetry is the security
    property, not an inconvenience: a receipt whose legs ANY verifier could
    reproduce -- which is what decrypting under a derivable identity scalar
    was -- is not evidence that a payment happened.
    """
    if role == "recipient":
        vds = {"vdNote": (eNote, mul(G1, v)),
               ("vdRec" if t == "note-a1" else "vdIss"): (eId, M_id)}
        for key, (E_expect, M_expect) in vds.items():
            rec = np.get(key)
            if rec is None:
                return f"{t}: recipient receipt needs {key}"
            E, M, acct, cid, vd = _vd_from_record(rec)
            if cid != core.chainid or acct != core.payee.addr_int:
                return f"{t}: {key} context mismatch"
            if not _ct_eq(E, E_expect):
                return f"{t}: {key} is about a different ciphertext"
            if not eq(M, M_expect):
                return f"{t}: {key} names the wrong plaintext"
            if not verifiable_decrypt_verify(E, pk_recv, M, vd, acct, cid):
                return f"{t}: {key} does not verify under pkRecv"
        return None

    # Issuer side: it cannot open its own ciphertexts (that is the whole
    # change), but it chose their randomness, so it discloses it and any
    # verifier recomputes them.  A wrong randomness cannot be salvaged: the
    # ciphertexts are fixed by the idHash the spend consumed.
    if "rNote" not in np or "rId" not in np:
        return f"{t}: issuer receipt needs the mint randomness (rNote, rId)"
    r_note, r_id = _h(np["rNote"]), _h(np["rId"])
    if not (_ct_eq(eNote, elgamal_encrypt(mul(G1, v), pk_recv, r_note))
            and _ct_eq(eId, elgamal_encrypt(M_id, pk_recv, r_id))):
        return f"{t}: the disclosed mint randomness does not produce these ciphertexts"
    return None


def _check_mailbox_binding(np: dict, M_rec) -> "str | None":
    """Check the mailbox leaf, if the receipt carries one.

    This is the holder-produced evidence that the key the note was addressed to
    is the registered mailbox of the named Identity -- a Poseidon and a path,
    checkable by anyone with no secret, which is exactly why the association is
    committed over the two POINTS as well as over the two scalars the spend
    proves ([[alberta_buck.registry.tree.mailbox_leaf]]).

    Tier 1 checks the leaf commits the named pair and the path folds to the
    root the binding states.  Whether that root was ever posted is tier 2.
    """
    b = np.get("binding")
    if b is None:
        return None
    pk_recv = _g1_from_hex(np["pkRecv"])
    try:
        leaf = mailbox_leaf(M_rec, pk_recv, _h(b["salt"]))
    except ValueError:
        return "mailbox binding: salt out of range"
    if leaf != _h(b["leaf"]):
        return "mailbox binding: leaf does not commit (M_rec, pkRecv, salt)"
    cur = leaf
    for sib, bit in zip(b["siblings"], b["indexBits"]):
        sib = _h(sib)
        cur = poseidon([cur, sib]) if int(bit) == 0 else poseidon([sib, cur])
    if cur != _h(b["root"]):
        return "mailbox binding: path does not fold to the stated root"
    return None


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
    """Tier-1 offline verification of an AB-RCPT/2 receipt core.

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
        # Approve handshake (soundness)
        E_payer = _ct_from_hex(ap["E_a"])
        E_spender = _ct_from_hex(ap["E_b"])
        pk_payer = _g1_from_hex(ap["pk_a"])
        pk_spender = _g1_from_hex(ap["pk_b"])
        sender = _h(ap["sender"])
        spender = _h(ap["spender"])
        cid_ap = ap["chainid"]
        registry = _h(core.contracts["registry"])
        p = ap["proof"]
        cp = CPProof(e=_h(p["e"]), s1=_h(p["s1"]), s2=_h(p["s2"]),
                     T1=_g1_from_hex(p["T1"]), T2=_g1_from_hex(p["T2"]),
                     T3=_g1_from_hex(p["T3"]))
        if not chaum_pedersen_verify(E_payer, E_spender, pk_payer, pk_spender,
                                      cp, sender, spender, cid_ap,
                                      registry):
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

        # The ISSUER's identity scalar is derivable by any verifier from the
        # disclosed preimage, and B1/A1 bind it into the leaf, so deriving it is
        # the naming.  The PAYEE's is deliberately not derived: it used to open
        # the addressed ciphertexts, which made naming a procedure anyone who
        # had ever seen a receipt could run.  That is the harvesting defect
        # stated as a feature, and the mailbox key is what removed it.
        m_iss = identity_scalar(core.payer.identity)

        # (b) Identity-M idHash preimage: the named parties are bound INTO the
        #     leaf the spend SNARK consumed.
        if t == "note-b1":
            sigma_R = _g1_from_hex(np["sigma_R"])
            sigma_s = _h(np["sigma_s"])
            if id_hash_b1(m_iss, sigma_R, sigma_s) != opening.id_hash:
                return RcptResult(False, None, None,
                                  "note-b1: idHash != id_hash_b1(m_iss, sigma)")
        elif t in ("note-a1", "note-a2"):
            eNote = _ct_from_hex(np["eNote"])
            if t == "note-a1":
                eId = _ct_from_hex(np["eRec"])
                sigma_R = _g1_from_hex(np["sigma_R"])
                sigma_s = _h(np["sigma_s"])
                if id_hash_a1(eNote, m_iss, sigma_R, sigma_s) != opening.id_hash:
                    return RcptResult(False, None, None,
                                      "note-a1: idHash != id_hash_a1(eNote, m_iss, sigma)")
                M_id, id_key = core.payee.M_pt, "vdRec"
            else:
                eId = _ct_from_hex(np["eIss"])
                if "T" not in np:
                    return RcptResult(False, None, None, "note-a2: missing T")
                if id_hash_a2(eNote, eId, _g1_from_hex(np["T"])) != opening.id_hash:
                    return RcptResult(False, None, None,
                                      "note-a2: idHash != id_hash_a2(eNote, eIss, T)")
                M_id, id_key = core.payer.M_pt, "vdIss"

            # The addressed legs are keyed to a MAILBOX, not to an Identity.
            # Nobody derives the opening secret from a disclosed record any
            # more -- which is the point -- so the receipt states the key and
            # the generating side proves what only it can.
            if "pkRecv" not in np:
                return RcptResult(False, None, None, f"{t}: missing pkRecv")
            pk_recv = _g1_from_hex(np["pkRecv"])

            err = _check_addressed_legs(t, np, role, pk_recv, eNote, eId,
                                        M_id, opening.v, core)
            if err is not None:
                return RcptResult(False, None, None, err)
            # The mailbox binding, when carried: it is what ties the key the
            # note was addressed to, to the person the receipt names.  An
            # issuer-side receipt cannot name the recipient without it, so its
            # absence is a banner rather than a failure -- the same treatment
            # A2's missing issuer binding gets.
            err = _check_mailbox_binding(np, core.payee.M_pt)
            if err is not None:
                return RcptResult(False, None, None, f"{t}: {err}")

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
                eIss = _ct_from_hex(np["eIss"])
                if not issuer_reenc_verify(core.payer.pk_pt, E_reg, eIss,
                                           binding, core.payer.addr_int, core.chainid):
                    return RcptResult(False, None, None, "note-a2: issuer binding fails")
                # The binding speaks of the T the leaf committed, and gamma opens
                # it: C_iss - T + gamma*H is the credential's plaintext, which
                # must be the Identity this receipt names.
                if not eq(binding.T, _g1_from_hex(np["T"])):
                    return RcptResult(False, None, None, "note-a2: binding T != committed T")
                if "gamma" not in np:
                    return RcptResult(False, None, None, "note-a2: binding carried without gamma")
                named = add(add(eIss.C, neg(binding.T)), mul(H_PEDERSEN, _h(np["gamma"]) % ORDER))
                if not eq(named, core.payer.M_pt):
                    return RcptResult(False, None, None,
                                      "note-a2: the binding's Identity is not the named issuer")
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

    flags = []
    if t == "note-a2" and core.issuer_binding is None:
        flags.append("UNVERIFIED ISSUER")
    if (t in ("note-a1", "note-a2") and role == "issuer"
            and (core.note or {}).get("binding") is None):
        # The issuer proved which MAILBOX it paid.  Only the accumulator ties a
        # mailbox to a person, and that evidence is the recipient's to give.
        flags.append("UNVERIFIED RECIPIENT")
    status = " + ".join(flags) if flags else "VALID"
    return RcptResult(True, core.payer.M_pt, core.txn.value, status)


__all__ = ["verify_receipt"]
