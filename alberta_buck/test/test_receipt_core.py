"""End-to-end tests for the AB-RCPT/2 receipt core — build, serialize,
re-parse, re-verify for all receipt kinds, from BOTH Note parties' sides.

Round-trip: build_* → serialize_core → canonical bytes → deserialize_core →
verify_receipt → RcptResult.  Checks the bit-identical property (same inputs →
same bytes → same receipt_id), that issuer- and recipient-generated Note
receipts share an identical Identity-M note payload (only the generator's
self-naming proof differs), and that tampering with any Identity-M leg —
the idHash preimage, the addressed ciphertexts, the A2 binding — rejects.
"""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from alberta_buck.wallet.bn254 import (
    G1, ORDER, mul, rand_scalar, words_to_point,
)
from alberta_buck.wallet.poseidon import F_R
from alberta_buck.wallet.elgamal import ElGamalCiphertext, elgamal_encrypt
from alberta_buck.wallet.chaum_pedersen import CPProof
from alberta_buck.wallet.schnorr import batch_commitment, issuer_schnorr_sign
from alberta_buck.wallet.issuer_reenc import issuer_reenc_prove
from alberta_buck.wallet.recvkey import receiving_key, prove_receiving_binding
from alberta_buck.wallet.salt import derive_salt
from alberta_buck.registry.tree import IdentityMerkleTree
from alberta_buck.wallet.notes import (
    NoteOpening, FLAVOR_A1, FLAVOR_A2, FLAVOR_B1,
    note_commitment, nullifier,
    id_hash_a1, id_hash_a2, id_hash_b1,
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


CHAINID   = 1
CONTRACTS = {"registry": "0x" + "1d" * 20, "buck": "0x" + "b0" * 20,
             "notes": "0x" + "70" * 20}
FACE      = 250


def _h(s: str) -> int:
    return int(s, 16)


def _pt(d):
    return words_to_point(_h(d["x"]), _h(d["y"]))


def _ct(d):
    return ElGamalCiphertext(R=_pt(d["R"]), C=_pt(d["C"]))


def _rng(seed: int):
    r = random.Random(seed)
    return lambda: r.getrandbits(256)


@pytest.fixture(scope="module")
def vectors():
    return build_vectors()


@pytest.fixture(scope="module")
def parties(vectors):
    """The canonical Alice (recipient/depositor) and Bob (issuer) records."""
    def mk(p, addr):
        return {
            "addr":     addr,
            "identity": p["canonical_identity_data"],
            "m":        _h(p["m"]),
            "M":        _pt(p["M"]),
            "pk":       _pt(p["elgamal_kp"]["pk"]),
            "sk":       _h(p["elgamal_kp"]["sk"]),
            "E":        _ct(p["ciphertext"]),
        }
    alice = mk(vectors["alice"], 0xa11ce00000000000000000000000000000a11ce)
    bob   = mk(vectors["bob"],   0x0b0b000000000000000000000000000000000b0b)
    # Alice's mailbox: an independent secret, and the leaf that ties it to her
    # Identity.  A note is addressed to the key; a payer checks the leaf.
    seed = 0xA11CE_5EED
    alice["k_recv"], alice["pk_recv"] = receiving_key(seed)
    salt = derive_salt(seed, "mailbox")
    tree = IdentityMerkleTree(depth=10, private=True)
    tree.insert_mailbox(alice["M"], alice["pk_recv"], salt)
    alice["mbx"] = prove_receiving_binding(alice["M"], alice["pk_recv"], salt, tree)
    return alice, bob


# ---- Identity-M mint/spend artifacts (tracer bullets, built locally) -------

def _mint_b1(alice, bob, rng):
    """Bob (public issuer) mints a bearer note; at spend Alice published
    eDepForIss (her Identity under Bob's registered key) in SpentCoupledB1."""
    rho     = rand_scalar(rng)
    idh     = id_hash_b1(bob["m"])
    opening = NoteOpening(FLAVOR_B1, FACE, rho, idh, 0)
    cm      = note_commitment(opening)
    cms     = [rand_scalar(rng) % F_R, cm]
    sig     = issuer_schnorr_sign(bob["sk"], batch_commitment(cms),
                                  bob["addr"], CHAINID, rng=rng)
    eDep    = elgamal_encrypt(alice["M"], bob["pk"], rand_scalar(rng))
    return dict(opening=opening, cms=cms, issuer_sig=sig,
                nullifier=nullifier(rho, idh), eDepForIss=eDep)


def _mint_a1(alice, bob, rng):
    """Bob (public issuer) addresses a note to Alice's MAILBOX key."""
    rho     = rand_scalar(rng)
    r_note  = rand_scalar(rng)
    eNote   = elgamal_encrypt(mul(G1, FACE), alice["pk_recv"], r_note)
    r_rec   = rand_scalar(rng)
    eRec    = elgamal_encrypt(alice["M"], alice["pk_recv"], r_rec)
    idh     = id_hash_a1(eNote, bob["m"])
    opening = NoteOpening(FLAVOR_A1, FACE, rho, idh, 0)
    cm      = note_commitment(opening)
    cms     = [cm, rand_scalar(rng) % F_R]
    sig     = issuer_schnorr_sign(bob["sk"], batch_commitment(cms),
                                  bob["addr"], CHAINID, rng=rng)
    return dict(opening=opening, cms=cms, issuer_sig=sig,
                eNote=eNote, eRec=eRec,
                r_note=r_note, r_id=r_rec,
                nullifier=nullifier(rho, idh))


def _mint_a2(alice, bob, rng, with_binding=True):
    """Bob (PRIVATE issuer) addresses a note to Alice's identity point,
    encrypting his own registered Identity in eIss + the mint binding."""
    rho     = rand_scalar(rng)
    r_note  = rand_scalar(rng)
    eNote   = elgamal_encrypt(mul(G1, FACE), alice["pk_recv"], r_note)
    r_prime = rand_scalar(rng)
    eIss    = elgamal_encrypt(bob["M"], alice["pk_recv"], r_prime)
    gamma   = rand_scalar(rng)
    binding = issuer_reenc_prove(bob["sk"], r_prime, alice["pk_recv"], bob["E"],
                                 eIss, bob["addr"], CHAINID, gamma=gamma, rng=rng)
    idh     = id_hash_a2(eNote, eIss, binding.T)
    opening = NoteOpening(FLAVOR_A2, FACE, rho, idh, 0)
    cm      = note_commitment(opening)
    cms     = [cm]
    # An unbound receipt still states T -- idHash commits it -- but not the proof.
    return dict(opening=opening, cms=cms, eNote=eNote, eIss=eIss,
                binding=binding if with_binding else None, T=binding.T, gamma=gamma,
                r_note=r_note, r_id=r_prime, nullifier=nullifier(rho, idh))


def _txn_kw(prefix: str) -> dict:
    return dict(value=FACE, block_time=1779999000,
                txhash="0x" + prefix * 32, block=1234599, logindex=1,
                mint_txhash="0x" + "cc" * 32, mint_block=1234500)


def _party_kw(alice, bob) -> dict:
    return dict(
        chainid=CHAINID, contracts=CONTRACTS,
        issuer_addr=bob["addr"], issuer_identity=bob["identity"],
        issuer_M=bob["M"], issuer_pk=bob["pk"],
        payee_addr=alice["addr"], payee_identity=alice["identity"],
        payee_M=alice["M"], payee_pk=alice["pk"], payee_E_addr=alice["E"],
    )


def _b1_core(alice, bob, role, rng):
    a = _mint_b1(alice, bob, rng)
    return build_note_b1(
        opening=a["opening"], cms=a["cms"], issuer_sig=a["issuer_sig"],
        nullifier=a["nullifier"], face=FACE,
        eDepForIss=a["eDepForIss"],
        role=role, payee_sk=alice["sk"], issuer_sk=bob["sk"],
        rng=rng, **_party_kw(alice, bob), **_txn_kw("b1"))


def _a1_core(alice, bob, role, rng):
    a = _mint_a1(alice, bob, rng)
    return build_note_a1(
        opening=a["opening"], cms=a["cms"], issuer_sig=a["issuer_sig"],
        eNote=a["eNote"], eRec=a["eRec"],
        nullifier=a["nullifier"], face=FACE,
        role=role, payee_sk=alice["sk"],
        pk_recv=alice["pk_recv"], mailbox_binding=alice["mbx"],
        k_recv=(alice["k_recv"] if role == "recipient" else None),
        r_note=(None if role == "recipient" else a["r_note"]),
        r_id=(None if role == "recipient" else a["r_id"]),
        rng=rng, **_party_kw(alice, bob), **_txn_kw("a1"))


def _a2_core(alice, bob, role, rng, with_binding=True):
    a = _mint_a2(alice, bob, rng, with_binding=with_binding)
    return build_note_a2(
        issuer_E_addr=bob["E"],
        opening=a["opening"], cms=a["cms"],
        eNote=a["eNote"], eIss=a["eIss"], binding=a["binding"], T=a["T"], gamma=a["gamma"],
        nullifier=a["nullifier"], face=FACE,
        role=role, payee_sk=alice["sk"], issuer_sk=bob["sk"],
        pk_recv=alice["pk_recv"], mailbox_binding=alice["mbx"],
        k_recv=(alice["k_recv"] if role == "recipient" else None),
        r_note=(None if role == "recipient" else a["r_note"]),
        r_id=(None if role == "recipient" else a["r_id"]),
        rng=rng, **_party_kw(alice, bob), **_txn_kw("a2"))


def _eoa_pub_core(alice, bob, rng):
    return build_eoa_pub(
        chainid=CHAINID, contracts=CONTRACTS,
        payer_addr=alice["addr"], payer_identity=alice["identity"],
        payer_M=alice["M"], payer_pk=alice["pk"],
        payee_addr=bob["addr"], payee_identity=bob["identity"],
        payee_M=bob["M"], payee_pk=bob["pk"],
        payee_sk=bob["sk"], payee_E_addr=bob["E"],
        value=500_000000, block_time=1779999000,
        txhash="0x" + "ea" * 32, block=1234567, logindex=2, rng=rng)


def _eoa_priv_core(vectors, alice, bob, rng):
    ap = vectors["approve"]
    cp = ap["cp_proof"]
    return build_eoa_priv(
        chainid=CHAINID, contracts=CONTRACTS,
        payer_addr=alice["addr"], payer_identity=alice["identity"],
        payer_M=alice["M"], payer_pk=alice["pk"], payer_E_addr=alice["E"],
        E_for_payee=_ct(ap["E_for_bob"]),
        cp_proof=CPProof(e=_h(cp["e"]), s1=_h(cp["s1"]), s2=_h(cp["s2"]),
                         T1=_pt(cp["T1"]), T2=_pt(cp["T2"]), T3=_pt(cp["T3"])),
        payee_addr=bob["addr"], payee_identity=bob["identity"],
        payee_M=bob["M"], payee_pk=bob["pk"],
        payee_sk=bob["sk"], payee_E_addr=bob["E"],
        value=500_000000, block_time=1779999000,
        txhash="0x" + "ee" * 32, block=1234567, logindex=2,
        rng=rng)


def _make(kind, vectors, alice, bob, seed=0x5eed):
    rng = _rng(seed)
    if kind == "eoa_pub":
        return _eoa_pub_core(alice, bob, rng)
    if kind == "eoa_priv":
        return _eoa_priv_core(vectors, alice, bob, rng)
    flavor, _, role = kind.partition(":")
    role = role or "recipient"
    return {"note_b1": _b1_core, "note_a1": _a1_core, "note_a2": _a2_core}[
        flavor](alice, bob, role, rng)


ALL_BUILDS = ["eoa_pub", "eoa_priv",
              "note_b1:recipient", "note_a1:recipient", "note_a2:recipient",
              "note_b1:issuer", "note_a1:issuer", "note_a2:issuer"]


# ---- round-trip: serialize ↔ deserialize ↔ verify --------------------------

@pytest.mark.parametrize("kind", ALL_BUILDS)
def test_roundtrip_serialize_deserialize_verify(vectors, parties, kind):
    alice, bob = parties
    core = _make(kind, vectors, alice, bob)

    # Serialize → canonical bytes
    b = serialize_core(core)
    assert isinstance(b, bytes) and len(b) > 0

    # Deserialize → ReceiptCore; re-serialize → same bits (bit-identical)
    core2 = deserialize_core(b)
    assert core2.type == core.type
    assert core2.role == core.role
    assert core2.payer.addr == core.payer.addr
    assert core2.payee.addr == core.payee.addr
    assert core2.txn.value == core.txn.value
    b2 = serialize_core(core2)
    assert b2 == b
    assert receipt_id(b) == receipt_id(b2)

    # Tier-1 verify: every kind is soundly bound under Identity-M.
    res = verify_receipt(core2)
    assert res.ok, f"{kind}: {res.reason}"
    assert res.reason == "VALID"


def test_bit_identical_rebuild(vectors, parties):
    """Same inputs, same canonical bytes — the deterministic-receipt property."""
    alice, bob = parties
    b1 = serialize_core(_make("note_a2:recipient", vectors, alice, bob))
    b2 = serialize_core(_make("note_a2:recipient", vectors, alice, bob))
    assert b1 == b2


@pytest.mark.parametrize("flavor", ["note_b1", "note_a1", "note_a2"])
def test_both_parties_share_note_payload(vectors, parties, flavor):
    """The note's own material is IDENTICAL from either side.

    The EVIDENCE about it is not, and for the addressed flavours it must not
    be: the recipient proves by verifiable decryption under its mailbox key,
    the issuer by disclosing the randomness it encrypted with, and neither can
    produce the other's.  A leg both sides could produce is a leg any reader
    could reproduce, which is what naming by a derivable identity scalar was.
    """
    import json
    alice, bob = parties
    rec = _make(f"{flavor}:recipient", vectors, alice, bob)
    iss = _make(f"{flavor}:issuer",    vectors, alice, bob)
    evidence = {"vdNote", "vdRec", "vdIss", "rNote", "rId"}
    shared = lambda d: {k: v for k, v in d.items() if k not in evidence}
    assert rec.note is not None and shared(rec.note) == shared(iss.note)
    assert not (set(rec.note) & set(iss.note) & evidence)
    assert rec.proof == iss.proof
    assert json.dumps(rec.txn.__dict__, sort_keys=True) == \
           json.dumps(iss.txn.__dict__, sort_keys=True)
    assert rec.issuer_binding == iss.issuer_binding
    # ...while the roles and self-namings are the two sides' own.
    assert (rec.role, iss.role) == ("recipient", "issuer")
    assert rec.payee_vd is not None and iss.payee_vd is None


# ---- envelope text round-trip -----------------------------------------------

@pytest.mark.parametrize("kind", ["eoa_pub", "note_b1:issuer", "note_a2:recipient"])
def test_envelope_roundtrip(vectors, parties, kind):
    alice, bob = parties
    core = _make(kind, vectors, alice, bob)
    b = serialize_core(core)

    env = envelope_text(b)
    assert env.startswith("AB-RCPT/2.")
    assert env.strip().endswith(".END")
    assert parse_envelope(env) == b


# ---- negative: tampered envelope rejected -----------------------------------

def test_parse_envelope_rejects_missing_header():
    with pytest.raises(ValueError, match="header"):
        parse_envelope("garbage\n.END")


def test_parse_envelope_rejects_missing_footer():
    with pytest.raises(ValueError, match="footer"):
        parse_envelope("AB-RCPT/2.\nZm9v\n")


# ---- verify the vector-emitted envelopes parse and verify -------------------

ALL_VECTOR_KINDS = ["eoa_pub", "eoa_priv", "note_b1", "note_a1", "note_a2",
                    "note_b1_issuer", "note_a1_issuer", "note_a2_issuer",
                    "eoa_pub_unicode"]


@pytest.mark.parametrize("kind", ALL_VECTOR_KINDS)
def test_vector_envelope_verifies(vectors, kind):
    """The abrcpt.<kind>.envelope in identity.json must parse and tier-1 verify."""
    env = vectors["abrcpt"][kind]["envelope"]
    core = deserialize_core(parse_envelope(env))
    assert core.v == 1
    assert core.type == "note-" + kind[5:7] if kind.startswith("note") else True
    assert core.role == ("issuer" if kind.endswith("_issuer") else "recipient")
    res = verify_receipt(core)
    assert res.ok, f"{kind}: {res.reason}"
    assert res.reason == "VALID"


def test_unicode_receipt_pins_the_canonical_dialect(vectors):
    """The eoa-pub-unicode receipt forces raw-UTF-8 handling end to end:
    the payer's canonical preimage (Latin accents + CJK) must be a
    canonical_json fixpoint, recompute to the named m/M, survive the
    base64url envelope round-trip byte-for-byte, and render its name."""
    import json as _json

    from alberta_buck.wallet.identity import canonical_json, identity_scalar
    from alberta_buck.wallet.bn254 import G1, mul, point_to_words
    from alberta_buck.wallet.envelope import envelope_text, serialize_core, receipt_id
    from alberta_buck.wallet.render import render_receipt, TextDriver

    entry = vectors["abrcpt"]["eoa_pub_unicode"]
    core = deserialize_core(parse_envelope(entry["envelope"]))

    # The preimage really is non-ASCII, raw (no \\uXXXX escape sequences).
    canonical = core.payer.identity
    assert "Chloé" in canonical and "Bélanger-李" in canonical
    assert "\\u" not in canonical

    # Canonical fixpoint + m/M recomputation over the UTF-8 bytes.
    assert canonical_json(_json.loads(canonical)) == canonical
    m = identity_scalar(canonical)
    assert mul(G1, m) == core.payer.M_pt

    # Envelope round-trip is byte-identical; receipt_id matches.
    b = serialize_core(core)
    assert envelope_text(b) == entry["envelope"]
    assert receipt_id(b) == entry["id"]

    # The rendered slip names the unicode payer.
    text = TextDriver(48).render(render_receipt(core))
    assert "Chloé" in text and "李" in text


# ---- Identity-M binding negatives -------------------------------------------

def test_unaddressed_identity_cannot_claim_a2(vectors, parties):
    """A note addressed to Alice's mailbox cannot be claimed by Bob.

    Bob holds his own key, not Alice's, so his verifiable decryption of the
    note's ciphertexts does not verify under the pkRecv the note states.  The
    check no longer rests on a derivable scalar, and it is stronger for it: an
    identity scalar is disclosed to every counterparty, a mailbox key to none.
    """
    alice, bob = parties
    rng = _rng(0x5eed)
    a = _mint_a2(alice, bob, rng)                  # addressed to Alice
    bob_k, bob_pk = receiving_key(0xB0B_5EED)
    core = build_note_a2(                          # ...claimed by Bob
        chainid=CHAINID, contracts=CONTRACTS,
        issuer_addr=bob["addr"], issuer_identity=bob["identity"],
        issuer_M=bob["M"], issuer_pk=bob["pk"], issuer_E_addr=bob["E"],
        payee_addr=bob["addr"], payee_identity=bob["identity"],
        payee_M=bob["M"], payee_pk=bob["pk"], payee_E_addr=bob["E"],
        opening=a["opening"], cms=a["cms"],
        eNote=a["eNote"], eIss=a["eIss"], binding=a["binding"], T=a["T"], gamma=a["gamma"],
        nullifier=a["nullifier"], face=FACE,
        role="recipient", payee_sk=bob["sk"],
        pk_recv=alice["pk_recv"], k_recv=bob_k,
        rng=rng, **_txn_kw("a2"))
    res = verify_receipt(core)
    assert not res.ok and "vdNote" in res.reason


def test_tampered_idhash_preimage_rejected(vectors, parties):
    """B1: naming a different issuer -- a consistent record and point -- breaks the
    idHash recomputation, because the named issuer is bound INTO the leaf."""
    import json
    from alberta_buck.wallet.envelope import _g1_hex
    from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
    alice, bob = parties
    core = _make("note_b1:recipient", vectors, alice, bob)
    other = canonical_identity_data(dict(json.loads(core.payer.identity), family_name="Other"))
    payer = replace(core.payer, identity=other, M=_g1_hex(mul(G1, identity_scalar(other))))
    res = verify_receipt(replace(core, payer=payer))
    assert not res.ok and "id_hash_b1" in res.reason


def test_a1_substituted_eRec_rejected(vectors, parties):
    """A1: substituting another identity's eRec breaks the leg that names the
    recipient -- the verifiable decryption is about a specific ciphertext, and
    a substituted one is not the one it is about."""
    alice, bob = parties
    core = _make("note_a1:recipient", vectors, alice, bob)
    rng = _rng(0xbad)
    bad_eRec = elgamal_encrypt(bob["M"], alice["pk_recv"], rand_scalar(rng))
    from alberta_buck.wallet.envelope import _ct_hex
    bad_note = dict(core.note)
    bad_note["eRec"] = _ct_hex(bad_eRec)
    res = verify_receipt(replace(core, note=bad_note))
    assert not res.ok and "vdRec" in res.reason


def test_a2_unbound_receipt_is_unverified(vectors, parties):
    """A2 without the mint binding still verifies but is stamped UNVERIFIED."""
    alice, bob = parties
    core = _a2_core(alice, bob, "recipient", _rng(0x5eed), with_binding=False)
    assert core.issuer_binding_status == "unverified"
    res = verify_receipt(core)
    assert res.ok
    assert "UNVERIFIED" in (res.reason or "")


def test_a2_tampered_binding_rejected(vectors, parties):
    alice, bob = parties
    core = _make("note_a2:recipient", vectors, alice, bob)
    bad = dict(core.issuer_binding)
    bad["s_r"] = hex((int(bad["s_r"], 16) + 1) % ORDER)
    res = verify_receipt(replace(core, issuer_binding=bad))
    assert not res.ok and "binding fails" in res.reason


def test_b1_issuer_receipt_names_depositor(vectors, parties):
    """The issuer-side B1 receipt names the depositor via the event's
    eDepForIss; a vd naming a different M rejects."""
    alice, bob = parties
    core = _make("note_b1:issuer", vectors, alice, bob)
    assert core.vd_payee is not None
    res = verify_receipt(core)
    assert res.ok and res.reason == "VALID"
    # Tamper: claim the depositor was Bob.
    from alberta_buck.wallet.envelope import _g1_hex
    bad_vd = dict(core.vd_payee)
    bad_vd["M_named"] = _g1_hex(bob["M"])
    bad_payee = replace(core.payee, identity=bob["identity"],
                        M=_g1_hex(bob["M"]))
    res = verify_receipt(replace(core, vd_payee=bad_vd, payee=bad_payee))
    assert not res.ok


def test_issuer_role_rejected_for_eoa(vectors, parties):
    alice, bob = parties
    core = _make("eoa_pub", vectors, alice, bob)
    res = verify_receipt(replace(core, role="issuer"))
    assert not res.ok and "Notes only" in res.reason
