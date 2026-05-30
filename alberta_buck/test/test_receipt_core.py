"""End-to-end tests for the AB-RCPT/1 receipt core — build, serialize, re-parse,
re-verify for all five receipt kinds.

Round-trip: build_* → serialize_core → canonical bytes → deserialize_core →
verify_receipt → RcptResult.  Checks bit-identical property (same inputs →
same bytes → same receipt_id).  Checks the four fully-shipped kinds pass
verification; A2 passes with UNVERIFIED ISSUER status pending Phase 2.
"""

from __future__ import annotations

import pytest

from alberta_buck.wallet.bn254 import ORDER, words_to_point, scalar_to_hex
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.chaum_pedersen import CPProof
from alberta_buck.wallet.verifiable_decrypt import VDProof
from alberta_buck.wallet.schnorr import SchnorrProof
from alberta_buck.wallet.notes import (
    NoteOpening, FLAVOR_A1, FLAVOR_A2, FLAVOR_B1,
    note_commitment, nullifier_a, nullifier_b,
)
from alberta_buck.wallet.build_receipt import (
    build_eoa_pub, build_eoa_priv,
    build_note_b1, build_note_a1, build_note_a2,
)
from alberta_buck.wallet.envelope import (
    serialize_core, deserialize_core,
    envelope_text, parse_envelope, receipt_id,
)
from alberta_buck.wallet.verify_receipt import verify_receipt
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


# ---- helpers: rebuild the cores from the vector data (tracer bullets) -----

def _eoa_pub_core(vectors):
    a = vectors["alice"]; b = vectors["bob"]
    return build_eoa_pub(
        chainid=1,
        contracts={"registry": "0x" + "1d" * 20, "buck": "0x" + "b0" * 20,
                   "notes": "0x" + "70" * 20},
        payer_addr=0xa11ce00000000000000000000000000000a11ce,
        payer_identity=a["canonical_identity_data"], payer_M=_pt(a["M"]),
        payer_pk=_pt(a["elgamal_kp"]["pk"]),
        payee_addr=0x0b0b000000000000000000000000000000000b0b,
        payee_identity=b["canonical_identity_data"], payee_M=_pt(b["M"]),
        payee_pk=_pt(b["elgamal_kp"]["pk"]), payee_sk=_h(b["elgamal_kp"]["sk"]),
        payee_E_addr=_ct(b["ciphertext"]),
        value=500_000000, block_time=1779999000,
        txhash="0x" + "ea" * 32, block=1234567, logindex=2,
    )


def _eoa_priv_core(vectors):
    a = vectors["alice"]; b = vectors["bob"]
    ap = vectors["approve"]
    cp = ap["cp_proof"]
    E_for_bob = ElGamalCiphertext(R=_pt(ap["E_for_bob"]["R"]),
                                   C=_pt(ap["E_for_bob"]["C"]))
    return build_eoa_priv(
        chainid=1,
        contracts={"registry": "0x" + "1d" * 20, "buck": "0x" + "b0" * 20,
                   "notes": "0x" + "70" * 20},
        payer_addr=_h(ap["sender"]), payer_identity=a["canonical_identity_data"],
        payer_M=_pt(a["M"]), payer_pk=_pt(a["elgamal_kp"]["pk"]),
        payer_E_addr=_ct(a["ciphertext"]),
        E_for_payee=E_for_bob,
        cp_proof=CPProof(
            e=_h(cp["e"]), s1=_h(cp["s1"]), s2=_h(cp["s2"]),
            T1=_pt(cp["T1"]), T2=_pt(cp["T2"]), T3=_pt(cp["T3"]),
        ),
        payee_addr=_h(ap["spender"]), payee_identity=b["canonical_identity_data"],
        payee_M=_pt(b["M"]), payee_pk=_pt(b["elgamal_kp"]["pk"]),
        payee_sk=_h(b["elgamal_kp"]["sk"]), payee_E_addr=_ct(b["ciphertext"]),
        value=500_000000, block_time=1779999000,
        txhash="0x" + "ee" * 32, block=1234567, logindex=2,
    )


def _note_b1_core(vectors):
    a = vectors["alice"]; b = vectors["bob"]; r = vectors["receipt"]
    o = r["opening"]; s = r["issuer_sig"]
    return build_note_b1(
        chainid=1,
        contracts={"registry": "0x" + "1d" * 20, "buck": "0x" + "b0" * 20,
                   "notes": "0x" + "70" * 20},
        issuer_addr=_h(r["issuer"]), issuer_identity=b["canonical_identity_data"],
        issuer_M=_pt(r["issuer_M"]), issuer_pk=_pt(r["issuer_pk"]),
        payee_addr=_h(r["recipient"]), payee_identity=a["canonical_identity_data"],
        payee_M=_pt(a["M"]), payee_pk=_pt(a["elgamal_kp"]["pk"]),
        payee_sk=_h(a["elgamal_kp"]["sk"]), payee_E_addr=_ct(a["ciphertext"]),
        opening=NoteOpening(flavor=_h(o["flavor"]), v=_h(o["v"]),
                            rho=_h(o["rho"]), id_hash=_h(o["idHash"]),
                            predicate=_h(o["predicate"])),
        cms=[_h(c) for c in r["cms"]],
        issuer_sig=SchnorrProof(e=_h(s["e"]), s=_h(s["s"]), R=_pt(s["R"])),
        nullifier=_h(r["nullifier"]), face=_h(r["face"]),
        value=_h(r["face"]), block_time=1779999000,
        txhash="0x" + "b1" * 32, block=1234599, logindex=1,
        mint_txhash="0x" + "bb" * 32, mint_block=1234500,
    )


def _note_a1_core(vectors):
    # Uses the same payload as B1 but with flavor A1
    a = vectors["alice"]; b = vectors["bob"]; r = vectors["receipt"]
    o = r["opening"]; s = r["issuer_sig"]
    opening = NoteOpening(flavor=FLAVOR_A1, v=_h(o["v"]),
                          rho=_h(o["rho"]), id_hash=_h(o["idHash"]),
                          predicate=_h(o["predicate"]))
    cm = note_commitment(opening)
    cms = [_h(c) for c in r["cms"][:2]] + [cm]
    from alberta_buck.wallet.schnorr import batch_commitment, issuer_schnorr_sign
    h_b = batch_commitment(cms)
    sig = issuer_schnorr_sign(_h(b["elgamal_kp"]["sk"]), h_b, _h(r["issuer"]), 1)
    nf = nullifier_a(opening.rho, opening.id_hash)
    return build_note_a1(
        chainid=1,
        contracts={"registry": "0x" + "1d" * 20, "buck": "0x" + "b0" * 20,
                   "notes": "0x" + "70" * 20},
        issuer_addr=_h(r["issuer"]), issuer_identity=b["canonical_identity_data"],
        issuer_M=_pt(r["issuer_M"]), issuer_pk=_pt(r["issuer_pk"]),
        payee_addr=_h(r["recipient"]), payee_identity=a["canonical_identity_data"],
        payee_M=_pt(a["M"]), payee_pk=_pt(a["elgamal_kp"]["pk"]),
        payee_sk=_h(a["elgamal_kp"]["sk"]), payee_E_addr=_ct(a["ciphertext"]),
        opening=opening, cms=cms, issuer_sig=sig,
        nullifier=nf, face=_h(r["face"]),
        value=_h(r["face"]), block_time=1779999000,
        txhash="0x" + "a1" * 32, block=1234599, logindex=1,
        mint_txhash="0x" + "aa" * 32, mint_block=1234500,
    )


def _note_a2_core(vectors):
    a = vectors["alice"]; b = vectors["bob"]; r = vectors["receipt"]
    from alberta_buck.wallet.elgamal import elgamal_encrypt
    from alberta_buck.wallet.bn254 import rand_scalar
    import random
    rng = lambda: random.Random(0x42).getrandbits(256)
    E_iss = elgamal_encrypt(_pt(b["M"]), _pt(a["elgamal_kp"]["pk"]), rand_scalar(rng))
    o = r["opening"]
    opening = NoteOpening(flavor=FLAVOR_A2, v=_h(o["v"]),
                          rho=_h(o["rho"]), id_hash=_h(o["idHash"]),
                          predicate=_h(o["predicate"]))
    nf = nullifier_a(opening.rho, opening.id_hash)
    return build_note_a2(
        chainid=1,
        contracts={"registry": "0x" + "1d" * 20, "buck": "0x" + "b0" * 20,
                   "notes": "0x" + "70" * 20},
        issuer_addr=_h(r["issuer"]), issuer_identity=b["canonical_identity_data"],
        issuer_M=_pt(b["M"]), issuer_pk=_pt(b["elgamal_kp"]["pk"]),
        issuer_E_addr=_ct(b["ciphertext"]),
        E_iss_for_rec=E_iss,
        payee_addr=_h(r["recipient"]), payee_identity=a["canonical_identity_data"],
        payee_M=_pt(a["M"]), payee_pk=_pt(a["elgamal_kp"]["pk"]),
        payee_sk=_h(a["elgamal_kp"]["sk"]), payee_E_addr=_ct(a["ciphertext"]),
        value=_h(r["face"]), block_time=1779999000,
        txhash="0x" + "a2" * 32, block=1234599, logindex=1,
        mint_txhash="0x" + "aa" * 32, mint_block=1234500, nullifier=nf,
    )


# ---- round-trip: serialize ↔ deserialize ↔ verify --------------------------

@pytest.mark.parametrize("kind", ["eoa_pub", "eoa_priv", "note_b1", "note_a1", "note_a2"])
def test_roundtrip_serialize_deserialize_verify(vectors, kind):
    builders = {
        "eoa_pub": _eoa_pub_core, "eoa_priv": _eoa_priv_core,
        "note_b1": _note_b1_core, "note_a1": _note_a1_core,
        "note_a2": _note_a2_core,
    }
    core = builders[kind](vectors)

    # Serialize → canonical bytes
    b = serialize_core(core)
    assert isinstance(b, bytes)
    assert len(b) > 0

    # Deserialize → ReceiptCore
    core2 = deserialize_core(b)
    assert core2.v == core.v
    assert core2.type == core.type
    assert core2.payer.addr == core.payer.addr
    assert core2.payee.addr == core.payee.addr
    assert core2.txn.value == core.txn.value

    # Re-serialize → same bits (bit-identical)
    b2 = serialize_core(core2)
    assert b2 == b

    # receipt_id is deterministic
    assert receipt_id(b) == receipt_id(b2)

    # Tier-1 verify
    res = verify_receipt(core2)
    if kind == "note_a2":
        # A2 passes verification but with UNVERIFIED ISSUER status
        assert res.ok, res.reason
        assert "UNVERIFIED" in (res.reason or "")
    else:
        assert res.ok, res.reason
        assert (res.reason or "").startswith("VALID") or res.reason == ""


# ---- envelope text round-trip -----------------------------------------------

@pytest.mark.parametrize("kind", ["eoa_pub", "eoa_priv", "note_b1", "note_a1"])
def test_envelope_roundtrip(vectors, kind):
    builders = {
        "eoa_pub": _eoa_pub_core, "eoa_priv": _eoa_priv_core,
        "note_b1": _note_b1_core, "note_a1": _note_a1_core,
    }
    core = builders[kind](vectors)
    b = serialize_core(core)

    # Envelope text
    env = envelope_text(b)
    assert env.startswith("AB-RCPT/1.")
    assert env.strip().endswith(".END")

    # Parse back
    b_parsed = parse_envelope(env)
    assert b_parsed == b

    # The envelope embeds whitespace — parse_envelope must survive it
    core3 = deserialize_core(b_parsed)
    assert core3.type.replace("-", "_") == kind


# ---- negative: tampered envelope rejected -----------------------------------

def test_parse_envelope_rejects_missing_header():
    import pytest as _p
    with _p.raises(ValueError, match="header"):
        parse_envelope("garbage\n.END")


def test_parse_envelope_rejects_missing_footer():
    import pytest as _p
    with _p.raises(ValueError, match="footer"):
        parse_envelope("AB-RCPT/1.\nZm9v\n")


# ---- verify the vector-emitted envelopes parse and verify -------------------

@pytest.mark.parametrize("kind", ["eoa_pub", "eoa_priv", "note_b1", "note_a1", "note_a2"])
def test_vector_envelope_verifies(vectors, kind):
    """The abrcpt.<kind>.envelope in identity.json must parse and tier-1 verify."""
    env = vectors["abrcpt"][kind]["envelope"]
    b = parse_envelope(env)
    core = deserialize_core(b)
    assert core.v == 1
    assert core.type == ("note-b1" if kind == "note_b1" else
                         "note-a1" if kind == "note_a1" else
                         "note-a2" if kind == "note_a2" else
                         "eoa-pub" if kind == "eoa_pub" else
                         "eoa-priv")
    res = verify_receipt(core)
    assert res.ok, f"{kind}: {res.reason}"
