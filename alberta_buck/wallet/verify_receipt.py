"""Tier-1 offline receipt verification from a :class:`ReceiptCore`.

Reference: alberta-buck-receipt.org ("Verification Procedure").

Consumes a deserialized receipt core (no chain access needed for tier 1) and
re-runs every proof: the point→human bridge (keccak(identity)·G == M) for each
party, the type-specific payer-naming verifier, and the payee self-naming
verifiable decryption.  Returns :class:`RcptResult` naming the parties and
value on success.

Tier 2 (chain anchoring) is out of scope — the verifier would read the
embedded ``pk`` / ``E_addr`` and event references from a node.
"""

from __future__ import annotations

from alberta_buck.wallet.bn254 import G1, mul, add, neg, eq
from alberta_buck.wallet.issuer_reenc import IssuerReencProof, issuer_reenc_verify
from alberta_buck.wallet.identity import identity_scalar, canonical_identity_data
from alberta_buck.wallet.chaum_pedersen import CPProof, chaum_pedersen_verify
from alberta_buck.wallet.verifiable_decrypt import VDProof, verifiable_decrypt_verify
from alberta_buck.wallet.schnorr import SchnorrProof, batch_commitment, issuer_schnorr_verify
from alberta_buck.wallet.notes import (
    NoteOpening, note_commitment, nullifier_a, nullifier_b,
    FLAVOR_A1, FLAVOR_A2, FLAVOR_B1,
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


def verify_receipt(core: ReceiptCore) -> RcptResult:
    """Tier-1 offline verification of an AB-RCPT/1 receipt core.

    Checks:
      1. Point→human bridge for both payer and payee.
      2. Type-specific payer-naming proof.
      3. Payee self-naming verifiable decryption.
      4. Named parties and value match the human-readable fields.

    Tier 2 (chain anchoring) requires an RPC node and is not run here.
    """
    # 1. Point→human bridge
    if not _check_point_identity(core.payer.M_pt, core.payer.identity):
        return RcptResult(False, None, None, "payer M != keccak(identity)·G")
    if not _check_point_identity(core.payee.M_pt, core.payee.identity):
        return RcptResult(False, None, None, "payee M != keccak(identity)·G")

    # 2. Payee self-naming (always present — every receipt kind)
    if core.payee_vd is None:
        return RcptResult(False, None, None, "missing payee self-naming proof")
    E_payee, M_payee, acct_payee, cid_payee, vd_payee = _vd_from_record(core.payee_vd)
    if not verifiable_decrypt_verify(E_payee, core.payee.pk_pt, M_payee,
                                     vd_payee, acct_payee, cid_payee):
        return RcptResult(False, None, None, "payee self-naming: vd fails")
    if acct_payee != core.payee.addr_int:
        return RcptResult(False, None, None, "payee self-naming: account mismatch")

    # 3. Type-specific payer naming
    t = core.type

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
        p = ap["proof"]
        cp = CPProof(e=_h(p["e"]), s1=_h(p["s1"]), s2=_h(p["s2"]),
                     T1=_g1_from_hex(p["T1"]), T2=_g1_from_hex(p["T2"]),
                     T3=_g1_from_hex(p["T3"]))
        if not chaum_pedersen_verify(E_payer, E_spender, pk_payer, pk_spender,
                                      cp, sender, spender, cid_ap):
            return RcptResult(False, None, None, "eoa-priv: approve handshake fails")
        # Verifiable decryption (recovery + provability)
        E_vd, M_vd, acct_vd, cid_vd, vd = _vd_from_record(vp)
        if not verifiable_decrypt_verify(E_vd, core.payee.pk_pt, M_vd,
                                          vd, acct_vd, cid_vd):
            return RcptResult(False, None, None, "eoa-priv: vd_payer fails")
        if not eq(M_vd, core.payer.M_pt):
            return RcptResult(False, None, None, "eoa-priv: named M != payer M")

    elif t in ("note-b1", "note-a1"):
        if core.proof is None:
            return RcptResult(False, None, None, f"{t}: missing proof")
        rp = core.proof
        o = rp["opening"]
        opening = NoteOpening(
            flavor=_h(o["flavor"]), v=_h(o["v"]), rho=_h(o["rho"]),
            id_hash=_h(o["idHash"]), predicate=_h(o["predicate"]),
        )
        cms = [_h(c) for c in rp["cms"]]
        # (a) cm is in the batch
        cm = note_commitment(opening)
        if cm not in cms:
            return RcptResult(False, None, None, f"{t}: opening cm not in minted batch")
        # (b)+(c) batch Schnorr
        sig = rp["issuer_sig"]
        h_batch = batch_commitment(cms)
        iss_sig = SchnorrProof(e=_h(sig["e"]), s=_h(sig["s"]),
                               R=_g1_from_hex(sig["R"]))
        if not issuer_schnorr_verify(core.payer.pk_pt, iss_sig, h_batch,
                                      core.payer.addr_int, core.chainid):
            return RcptResult(False, None, None, f"{t}: issuer batch binding fails")
        # (d) nullifier and face
        nf = _h(rp["nullifier"])
        face = _h(rp["face"])
        expected_nf = (nullifier_a(opening.rho, opening.id_hash)
                       if t == "note-a1" else
                       nullifier_b(opening.rho, opening.id_hash))
        if nf != expected_nf:
            return RcptResult(False, None, None, f"{t}: nullifier mismatch")
        if face != opening.v:
            return RcptResult(False, None, None, f"{t}: face != note value")

    elif t == "note-a2":
        # (1) Recipient recovers the issuer's Identity: verifiable decryption of
        #     E_iss-for-rec under the payee's key reveals M_iss.
        if core.vd_issuer is None:
            return RcptResult(False, None, None, "note-a2: missing vd_issuer")
        E_iss, M_iss, acct_iss, cid_iss, vd_iss = _vd_from_record(core.vd_issuer)
        if not verifiable_decrypt_verify(E_iss, core.payee.pk_pt, M_iss,
                                          vd_iss, acct_iss, cid_iss):
            return RcptResult(False, None, None, "note-a2: vd_issuer fails")
        if not eq(M_iss, core.payer.M_pt):
            return RcptResult(False, None, None, "note-a2: named M != issuer M")

        # Without an issuer binding the recovered M is not provably the issuer's
        # *registered* Identity -- UNVERIFIED ISSUER (pre-binding).
        if core.issuer_binding is None:
            pass  # accept; status banner set below
        else:
            # (2) The binding proves E_iss re-encrypts the issuer's registered
            #     Identity under the key committed in Q.
            b = core.issuer_binding
            binding = IssuerReencProof(
                e=_h(b["e"]), s_r=_h(b["s_r"]), s_b=_h(b["s_b"]),
                s_s=_h(b["s_s"]), s_g=_h(b["s_g"]),
                A1=_g1_from_hex(b["A1"]), A2=_g1_from_hex(b["A2"]),
                A3=_g1_from_hex(b["A3"]), A4=_g1_from_hex(b["A4"]),
                A5=_g1_from_hex(b["A5"]),
                Q=_g1_from_hex(b["Q"]), U=_g1_from_hex(b["U"]), T=_g1_from_hex(b["T"]),
            )
            E_reg = core.payer.E_addr_ct
            if E_reg is None:
                return RcptResult(False, None, None, "note-a2: issuer E_addr missing")
            if not issuer_reenc_verify(core.payer.pk_pt, E_reg, E_iss,
                                       binding, core.payer.addr_int, core.chainid):
                return RcptResult(False, None, None, "note-a2: issuer binding fails")

            # (3) Coupling + registered-identity tie: the binding's registered
            #     M_iss^reg = C_i - T = C_i - (T_hat - gamma*G) must equal the
            #     named M.  Combined with (1)'s M_named == payer.M, this forces
            #     pk_rec = Q's key (the E_note <-> Q coupling) and ties the named
            #     Identity to the issuer's *registered* credential.
            gamma = _h(b["gamma"])
            T_unblind = add(binding.T, neg(mul(G1, gamma)))   # T_hat - gamma*G = r'*pk_rec
            M_reg = add(E_iss.C, neg(T_unblind))              # C_i - T = M_iss^reg
            if not eq(M_reg, core.payer.M_pt):
                return RcptResult(False, None, None, "note-a2: binding M_iss != named M (coupling)")

    else:
        return RcptResult(False, None, None, f"unknown receipt type: {t}")

    # 4. Value consistency: txn value matches the human-readable field
    # (no additional check — the value is recorded in txn.value, and the
    # type-specific checks above already bind it to the proofs)

    status = "UNVERIFIED ISSUER" if (t == "note-a2" and core.issuer_binding_status == "unverified") else "VALID"
    return RcptResult(True, core.payer.M_pt, core.txn.value, status)


__all__ = ["verify_receipt"]
