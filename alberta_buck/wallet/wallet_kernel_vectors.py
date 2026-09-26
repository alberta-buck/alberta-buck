"""Cross-language WALLET kernel vectors.

Emits ``core/vectors/wallet-kernel-vectors.json`` from the pure-Python
reference path (``BUCK_IDENTITY_BACKEND=py``) so the Rust (``cargo``),
Python (``pytest``) and JS (``node --test``) suites can replay the
`buck-wallet` crate's surface bit-identically: THE canonical JSON
dialect, the AB-RCPT/2 envelope (receipt ids, wrapping, parsing), every
receipt-core builder for all five kinds from both generating sides, the
tier-1 verifier (positive, tampered and UNVERIFIED-ISSUER rows), the
unilateral A1/A2 flows, and the credential-issuer ceremony.

Every row records ALL inputs INCLUDING nonces, in the exact order the
reference draws them -- which is also the argument order of the
corresponding ``buck_wallet`` kernel function.  The rng stream here is
this module's own (fresh seed); the pinned streams behind
``vectors.py`` and ``kernel_vectors.py`` are untouched.

Regenerate: ``make nix-venv-core-wallet-vectors``.
"""

from __future__ import annotations

import json
import os
import random
from typing import Any, Callable, Dict, List

from alberta_buck.wallet.bn254 import (
    G1, ORDER, mul, point_to_words, rand_scalar, scalar_to_hex,
)
from alberta_buck.wallet.poseidon import F_R
from alberta_buck.wallet.identity import canonical_identity_data, canonical_json, identity_scalar
from alberta_buck.wallet.elgamal import elgamal_encrypt
from alberta_buck.wallet.chaum_pedersen import chaum_pedersen_prove
from alberta_buck.wallet.schnorr import batch_commitment, issuer_schnorr_sign
from alberta_buck.wallet.issuer_reenc import issuer_reenc_prove
from alberta_buck.wallet.ps import ps_keygen
from alberta_buck.wallet.notes import (
    NoteOpening, FLAVOR_A1, FLAVOR_A2, FLAVOR_B1,
    note_commitment, nullifier, id_hash_a1, id_hash_a2, id_hash_b1,
)
from alberta_buck.wallet.build_receipt import (
    build_eoa_pub, build_eoa_priv, build_note_b1, build_note_a1, build_note_a2,
)
from alberta_buck.wallet.envelope import (
    serialize_core, deserialize_core, envelope_text, parse_envelope, receipt_id,
    mailbox_binding_record,
)
from alberta_buck.wallet.verify_receipt import verify_receipt
from alberta_buck.registry.tree import IdentityMerkleTree
from alberta_buck.wallet.recvkey import prove_receiving_binding
from alberta_buck.wallet.issuer import Issuer
from alberta_buck.wallet.recvkey import receiving_key
from alberta_buck.wallet.transcript import keccak_raw
from alberta_buck.wallet.unilateral_a2 import (
    IdentityTree, mint_unilateral_a2, make_receipt, verify_receipt as verify_ua2,
)
from alberta_buck.wallet.unilateral_a1 import (
    mint_unilateral_a1, make_receipt_a1, verify_receipt_a1,
)


def _seeded_rng(seed: int) -> Callable[[], int]:
    rnd = random.Random(seed)
    return lambda: rnd.getrandbits(256)


def _replay(vals: List[int]) -> Callable[[], int]:
    it = iter(vals)
    return lambda: next(it)


def _hx(v: int) -> str:
    return f"0x{v:064x}"


def _g1(P) -> Dict[str, str]:
    x, y = point_to_words(P)
    return {"x": _hx(x), "y": _hx(y)}


def _ct(E) -> Dict[str, Any]:
    return {"R": _g1(E.R), "C": _g1(E.C)}


def _rcpt(res) -> Dict[str, Any]:
    return {
        "ok": res.ok,
        "reason": res.reason,
        "identity_M": _g1(res.identity_M) if res.identity_M is not None else None,
        "value": res.value,
    }


CHAINID = 1
CONTRACTS = {"registry": "0x" + "1d" * 20, "buck": "0x" + "b0" * 20,
             "notes": "0x" + "70" * 20}
REGISTRY = int(CONTRACTS["registry"], 16)
FACE = 250


def build_wallet_vectors(seed: int = 0x3A11E75EED) -> Dict[str, Any]:
    prev = os.environ.get("BUCK_IDENTITY_BACKEND")
    os.environ["BUCK_IDENTITY_BACKEND"] = "py"
    try:
        return _build(seed)
    finally:
        if prev is None:
            os.environ.pop("BUCK_IDENTITY_BACKEND", None)
        else:
            os.environ["BUCK_IDENTITY_BACKEND"] = prev


def _party(name: str, fields: Dict[str, Any], addr: int, draw) -> Dict[str, Any]:
    canonical = canonical_identity_data(fields)
    m = identity_scalar(canonical)
    M = mul(G1, m)
    sk = draw()
    pk = mul(G1, sk)
    r = draw()
    E = elgamal_encrypt(M, pk, r)
    # The party's wallet seed, and the Notes receiving key derived from it.
    # Derived from the party NAME rather than from the draw stream, so adding
    # it shifts no existing vector value, and -- more importantly -- never from
    # the identity: a reading key recoverable from a disclosed record is a
    # reading key everyone holding the record already has.
    wallet_seed = int.from_bytes(
        keccak_raw(b"AlbertaBuck/Vectors/WalletSeed/v2:" + name.encode()), "big"
    ) % ORDER or 1
    k_recv, pk_recv = receiving_key(wallet_seed)
    return {
        "name": name, "addr": addr, "identity": canonical, "m": m, "M": M,
        "sk": sk, "pk": pk, "r_E": r, "E": E,
        "wallet_seed": wallet_seed, "k_recv": k_recv, "pk_recv": pk_recv,
    }


def _party_json(p: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "addr": _hx(p["addr"]), "identity": p["identity"],
        "m": scalar_to_hex(p["m"]), "M": _g1(p["M"]),
        "sk": scalar_to_hex(p["sk"]), "pk": _g1(p["pk"]),
        "r_E": scalar_to_hex(p["r_E"]), "E": _ct(p["E"]),
        "wallet_seed": scalar_to_hex(p["wallet_seed"]),
        "k_recv": scalar_to_hex(p["k_recv"]), "pk_recv": _g1(p["pk_recv"]),
    }


def _opening_json(o: NoteOpening) -> Dict[str, str]:
    return {"flavor": _hx(o.flavor), "v": _hx(o.v), "rho": scalar_to_hex(o.rho),
            "idHash": _hx(o.id_hash), "predicate": _hx(o.predicate)}


def _schnorr_json(s) -> Dict[str, Any]:
    return {"e": scalar_to_hex(s.e), "s": scalar_to_hex(s.s), "R": _g1(s.R)}


def _binding_json(b) -> Dict[str, Any]:
    return {
        "e": scalar_to_hex(b.e), "s_r": scalar_to_hex(b.s_r),
        "s_b": scalar_to_hex(b.s_b), "s_s": scalar_to_hex(b.s_s),
        "s_g": scalar_to_hex(b.s_g),
        "A1": _g1(b.A1), "A2": _g1(b.A2), "A3": _g1(b.A3),
        "A4": _g1(b.A4), "A5": _g1(b.A5),
        "Q": _g1(b.Q), "U": _g1(b.U), "T": _g1(b.T),
    }


def _vd_json(v) -> Dict[str, Any]:
    return {"e": scalar_to_hex(v.e), "s": scalar_to_hex(v.s),
            "T1": _g1(v.T1), "T2": _g1(v.T2)}


def _finish(core) -> Dict[str, Any]:
    """Serialize, id, envelope and verify a built core -- the recorded
    outputs every suite replays."""
    blob = serialize_core(core)
    res = verify_receipt(deserialize_core(blob))
    return {
        "canonical": blob.decode("utf-8"),
        "receipt_id": receipt_id(blob),
        "envelope": envelope_text(blob),
        "verify": _rcpt(res),
    }


def _build(seed: int) -> Dict[str, Any]:
    rng = _seeded_rng(seed)
    draw = lambda: rand_scalar(rng)

    out: Dict[str, Any] = {
        "$schema_version": 1,
        "backend": "py",
        "seed": _hx(seed),
        "chainid": CHAINID,
        "contracts": CONTRACTS,
    }

    # ---- canonical JSON dialect -------------------------------------------
    tricky = [
        {"b": 1, "a": 2, "Z": 3, "aa": {"y": [1, 2, {"q": "x"}], "x": None}},
        {"given_name": "Zoë", "café": True, "n": 7},
        {"city": "Sainte-Thérèse", "note": "🍁 maple", "李": "CJK key"},
        {"ctrl": "line1\nline2\ttabbed\x01unit\x08bs\x0cff\r", "quote": "say \"hi\" \\ done"},
        {"big": 2**80 + 7, "neg": -(2**60), "small": 0, "bool_f": False},
        {},
    ]
    out["canonical_json"] = []
    for obj in tricky:
        pretty = json.dumps(obj, indent=2, ensure_ascii=False)
        canonical = canonical_json(obj)
        out["canonical_json"].append({
            "input": pretty,
            "canonical": canonical,
            "m": scalar_to_hex(identity_scalar(canonical)),
        })

    # ---- envelope mechanics -----------------------------------------------
    env_rows = []
    for obj in ({"v": 1, "hello": "wörld 🍁"}, {"v": 1, "n": list(range(40))}):
        blob = canonical_json(obj).encode("utf-8")
        env_rows.append({
            "canonical": blob.decode("utf-8"),
            "id12": receipt_id(blob),
            "id20": receipt_id(blob, 20),
            "envelope64": envelope_text(blob),
            "envelope8": envelope_text(blob, width=8),
            "noisy": "Dear payer,\r\n  keep this!\r\n"
                     + envelope_text(blob, width=13)
                     + "\r\n-- sincerely, the wallet\r\n",
        })
        assert parse_envelope(env_rows[-1]["noisy"]) == blob
    out["envelope"] = env_rows

    # ---- parties -----------------------------------------------------------
    alice = _party("alice", {"given_name": "Alice", "surname": "Ranch",
                             "dob": "1970-04-01", "issuer_id": "svc-alberta"},
                   0xA11CE00000000000000000000000000000A11CE, draw)
    bob = _party("bob", {"given_name": "Bob", "surname": "Granary",
                         "dob": "1968-11-11", "issuer_id": "svc-alberta"},
                 0x0B0B000000000000000000000000000000000B0B, draw)
    out["parties"] = {"alice": _party_json(alice), "bob": _party_json(bob)}

    txn_note = dict(value=FACE, block_time=1779999000, block=1234599,
                    logindex=1, mint_txhash="0x" + "cc" * 32, mint_block=1234500)
    party_kw = dict(
        chainid=CHAINID, contracts=CONTRACTS,
        issuer_addr=bob["addr"], issuer_identity=bob["identity"],
        issuer_M=bob["M"], issuer_pk=bob["pk"],
        payee_addr=alice["addr"], payee_identity=alice["identity"],
        payee_M=alice["M"], payee_pk=alice["pk"], payee_E_addr=alice["E"],
    )

    receipts: List[Dict[str, Any]] = []

    # ---- eoa-pub -----------------------------------------------------------
    t_self = draw()
    core = build_eoa_pub(
        chainid=CHAINID, contracts=CONTRACTS,
        payer_addr=alice["addr"], payer_identity=alice["identity"],
        payer_M=alice["M"], payer_pk=alice["pk"],
        payee_addr=bob["addr"], payee_identity=bob["identity"],
        payee_M=bob["M"], payee_pk=bob["pk"],
        payee_sk=bob["sk"], payee_E_addr=bob["E"],
        value=500_000000, block_time=1779999000,
        txhash="0x" + "ea" * 32, block=1234567, logindex=2,
        rng=_replay([t_self]))
    receipts.append({
        "kind": "eoa-pub", "role": "recipient",
        "payer": "alice", "payee": "bob",
        "txn": {"value": 500_000000, "block_time": 1779999000,
                "txhash": "0x" + "ea" * 32, "block": 1234567, "logindex": 2},
        "nonces": {"t_self": scalar_to_hex(t_self)},
        **_finish(core),
    })

    # ---- eoa-priv ----------------------------------------------------------
    r_prime = draw()
    E_for_bob = elgamal_encrypt(alice["M"], bob["pk"], r_prime)
    k1, k2 = draw(), draw()
    cp = chaum_pedersen_prove(alice["E"], E_for_bob, alice["pk"], bob["pk"],
                              alice["sk"], r_prime,
                              alice["addr"], bob["addr"], CHAINID,
                              rng=_replay([k1, k2]),
                              registry=REGISTRY)
    t_vd_payer, t_self = draw(), draw()
    core = build_eoa_priv(
        chainid=CHAINID, contracts=CONTRACTS,
        payer_addr=alice["addr"], payer_identity=alice["identity"],
        payer_M=alice["M"], payer_pk=alice["pk"], payer_E_addr=alice["E"],
        E_for_payee=E_for_bob, cp_proof=cp,
        payee_addr=bob["addr"], payee_identity=bob["identity"],
        payee_M=bob["M"], payee_pk=bob["pk"],
        payee_sk=bob["sk"], payee_E_addr=bob["E"],
        value=500_000000, block_time=1779999000,
        txhash="0x" + "ee" * 32, block=1234567, logindex=2,
        rng=_replay([t_vd_payer, t_self]))
    receipts.append({
        "kind": "eoa-priv", "role": "recipient",
        "payer": "alice", "payee": "bob",
        "E_for_payee": _ct(E_for_bob),
        "cp_proof": {"e": scalar_to_hex(cp.e), "s1": scalar_to_hex(cp.s1),
                     "s2": scalar_to_hex(cp.s2), "T1": _g1(cp.T1),
                     "T2": _g1(cp.T2), "T3": _g1(cp.T3)},
        "txn": {"value": 500_000000, "block_time": 1779999000,
                "txhash": "0x" + "ee" * 32, "block": 1234567, "logindex": 2},
        "nonces": {"r_prime": scalar_to_hex(r_prime),
                   "k1": scalar_to_hex(k1), "k2": scalar_to_hex(k2),
                   "t_vd_payer": scalar_to_hex(t_vd_payer),
                   "t_self": scalar_to_hex(t_self)},
        **_finish(core),
    })

    # ---- note-b1 (both roles over ONE mint) --------------------------------
    rho = draw()
    idh = id_hash_b1(bob["m"])
    opening = NoteOpening(FLAVOR_B1, FACE, rho, idh, 0)
    cm = note_commitment(opening)
    cms = [draw() % F_R, cm]
    k_sig = draw()
    sig = issuer_schnorr_sign(bob["sk"], batch_commitment(cms), bob["addr"],
                              CHAINID, rng=_replay([k_sig]))
    r_dep = draw()
    eDep = elgamal_encrypt(alice["M"], bob["pk"], r_dep)
    nf = nullifier(rho, idh)
    b1_mint = {
        "opening": _opening_json(opening), "cm": _hx(cm),
        "cms": [scalar_to_hex(c) for c in cms],
        "issuer_sig": _schnorr_json(sig),
        "eDepForIss": _ct(eDep), "nullifier": scalar_to_hex(nf),
        "nonces": {"rho": scalar_to_hex(rho), "k_sig": scalar_to_hex(k_sig),
                   "r_dep": scalar_to_hex(r_dep)},
    }
    for role in ("recipient", "issuer"):
        t_vd = draw()
        core = build_note_b1(
            opening=opening, cms=cms, issuer_sig=sig,
            nullifier=nf, face=FACE, eDepForIss=eDep,
            role=role, payee_sk=alice["sk"], issuer_sk=bob["sk"],
            txhash="0x" + "b1" * 32, rng=_replay([t_vd]),
            **party_kw, **txn_note)
        receipts.append({
            "kind": "note-b1", "role": role, "payer": "bob", "payee": "alice",
            "mint": b1_mint,
            "txn": {"txhash": "0x" + "b1" * 32, **txn_note},
            "nonces": {"t_vd": scalar_to_hex(t_vd)},
            **_finish(core),
        })

    # ---- note-a1 (both roles over ONE mint) --------------------------------
    rho = draw()
    r_note, r_rec = draw(), draw()
    # Keyed to the MAILBOX, not to the Identity: encrypting an identity point
    # under itself makes message and key share one secret, and one scalar
    # multiplication per candidate identifies the recipient.
    eNote = elgamal_encrypt(mul(G1, FACE), alice["pk_recv"], r_note)
    eRec = elgamal_encrypt(alice["M"], alice["pk_recv"], r_rec)
    idh = id_hash_a1(eNote, bob["m"])
    opening = NoteOpening(FLAVOR_A1, FACE, rho, idh, 0)
    cm = note_commitment(opening)
    cms = [cm, draw() % F_R]
    k_sig = draw()
    sig = issuer_schnorr_sign(bob["sk"], batch_commitment(cms), bob["addr"],
                              CHAINID, rng=_replay([k_sig]))
    nf = nullifier(rho, idh)
    a1_mint = {
        "opening": _opening_json(opening), "cm": _hx(cm),
        "cms": [scalar_to_hex(c) for c in cms],
        "issuer_sig": _schnorr_json(sig),
        "eNote": _ct(eNote), "eRec": _ct(eRec),
        "nullifier": scalar_to_hex(nf),
        "nonces": {"rho": scalar_to_hex(rho),
                   "r_note": scalar_to_hex(r_note),
                   "r_rec": scalar_to_hex(r_rec),
                   "k_sig": scalar_to_hex(k_sig)},
    }
    # The mailbox association, as the payer and the receipt verifier check it:
    # a leaf over the two POINTS, so no secret is needed and none is disclosed.
    mbx_salt = draw() % F_R or 1
    mbx_tree = IdentityMerkleTree(depth=10, private=True)
    mbx_tree.insert_mailbox(alice["M"], alice["pk_recv"], mbx_salt)
    mbx = prove_receiving_binding(alice["M"], alice["pk_recv"], mbx_salt, mbx_tree)
    for role in ("recipient", "issuer"):
        nonces = {}
        if role == "recipient":
            t_note, t_id, t_vd = draw(), draw(), draw()
            nonces = {"t_note": scalar_to_hex(t_note),
                      "t_id": scalar_to_hex(t_id),
                      "t_vd": scalar_to_hex(t_vd)}
            rng_role = _replay([t_note, t_id, t_vd])
        else:
            rng_role = _replay([])
        core = build_note_a1(
            opening=opening, cms=cms, issuer_sig=sig,
            eNote=eNote, eRec=eRec,
            nullifier=nf, face=FACE,
            role=role, payee_sk=alice["sk"],
            pk_recv=alice["pk_recv"],
            k_recv=(alice["k_recv"] if role == "recipient" else None),
            r_note=(None if role == "recipient" else r_note),
            r_id=(None if role == "recipient" else r_rec),
            mailbox_binding=mbx,
            txhash="0x" + "a1" * 32, rng=rng_role,
            **party_kw, **txn_note)
        receipts.append({
            "kind": "note-a1", "role": role, "payer": "bob", "payee": "alice",
            "mailboxBinding": mailbox_binding_record(mbx),
            "mint": a1_mint,
            "txn": {"txhash": "0x" + "a1" * 32, **txn_note},
            "nonces": nonces,
            **_finish(core),
        })

    # ---- note-a2 (both roles + the unbound row, over ONE mint) -------------
    rho, r_note = draw(), draw()
    eNote = elgamal_encrypt(mul(G1, FACE), alice["pk_recv"], r_note)
    r_prime = draw()
    eIss = elgamal_encrypt(bob["M"], alice["pk_recv"], r_prime)
    beta, gamma, k_r, k_b, k_s, k_g = (draw() for _ in range(6))
    binding = issuer_reenc_prove(bob["sk"], r_prime, alice["pk_recv"], bob["E"],
                                 eIss, bob["addr"], CHAINID,
                                 rng=_replay([k_r, k_b, k_s, k_g]),
                                 beta=beta, gamma=gamma)
    idh = id_hash_a2(eNote, eIss, binding.T)
    opening = NoteOpening(FLAVOR_A2, FACE, rho, idh, 0)
    cm = note_commitment(opening)
    cms = [cm]
    nf = nullifier(rho, idh)
    a2_mint = {
        "opening": _opening_json(opening), "cm": _hx(cm),
        "cms": [scalar_to_hex(c) for c in cms],
        "eNote": _ct(eNote), "eIss": _ct(eIss),
        "binding": _binding_json(binding),
        "nullifier": scalar_to_hex(nf),
        "nonces": {"rho": scalar_to_hex(rho),
                   "r_note": scalar_to_hex(r_note),
                   "r_prime": scalar_to_hex(r_prime),
                   "beta": scalar_to_hex(beta), "gamma": scalar_to_hex(gamma),
                   "k_r": scalar_to_hex(k_r), "k_b": scalar_to_hex(k_b),
                   "k_s": scalar_to_hex(k_s), "k_g": scalar_to_hex(k_g)},
    }
    a2_kw = dict(
        issuer_E_addr=bob["E"], opening=opening, cms=cms,
        eNote=eNote, eIss=eIss, nullifier=nf, face=FACE,
        payee_sk=alice["sk"], issuer_sk=bob["sk"],
        pk_recv=alice["pk_recv"], T=binding.T, gamma=gamma,
        txhash="0x" + "a2" * 32, **party_kw, **txn_note)
    for role in ("recipient", "issuer"):
        if role == "recipient":
            t_note, t_id, t_vd = draw(), draw(), draw()
            nonces = {"t_note": scalar_to_hex(t_note),
                      "t_id": scalar_to_hex(t_id),
                      "t_vd": scalar_to_hex(t_vd)}
            rng_role = _replay([t_note, t_id, t_vd])
        else:
            t_vd = draw()
            nonces = {"t_vd": scalar_to_hex(t_vd)}
            rng_role = _replay([t_vd])
        # A2's issuer row carries NO mailbox binding, deliberately: it pins the
        # UNVERIFIED RECIPIENT banner, which is what an issuer-side receipt
        # earns when nobody has tied the mailbox it paid to a person.
        core = build_note_a2(
            binding=binding, role=role, rng=rng_role,
            k_recv=(alice["k_recv"] if role == "recipient" else None),
            r_note=(None if role == "recipient" else r_note),
            r_id=(None if role == "recipient" else r_prime),
            mailbox_binding=(mbx if role == "recipient" else None),
            **a2_kw)
        receipts.append({
            "kind": "note-a2", "role": role, "payer": "bob", "payee": "alice",
            **({"mailboxBinding": mailbox_binding_record(mbx)}
               if role == "recipient" else {}),
            "mint": a2_mint,
            "txn": {"txhash": "0x" + "a2" * 32, **txn_note},
            "nonces": nonces,
            **_finish(core),
        })
    # UNVERIFIED ISSUER: the same mint without its binding.
    t_note, t_id, t_vd = draw(), draw(), draw()
    core = build_note_a2(binding=None, role="recipient",
                         rng=_replay([t_note, t_id, t_vd]),
                         k_recv=alice["k_recv"], mailbox_binding=mbx, **a2_kw)
    receipts.append({
        "kind": "note-a2-unbound", "role": "recipient",
        "payer": "bob", "payee": "alice", "mint": a2_mint,
        "mailboxBinding": mailbox_binding_record(mbx),
        "txn": {"txhash": "0x" + "a2" * 32, **txn_note},
        "nonces": {"t_note": scalar_to_hex(t_note),
                   "t_id": scalar_to_hex(t_id),
                   "t_vd": scalar_to_hex(t_vd)},
        **_finish(core),
    })

    out["receipts"] = receipts

    # ---- tampered rows: decode-modify-reserialize, expected rejections -----
    tampered = []

    def _tamper(base_row, mutate, note):
        d = json.loads(base_row["canonical"])
        mutate(d)
        blob = canonical_json(d).encode("utf-8")
        res = verify_receipt(deserialize_core(blob))
        assert not res.ok, f"tamper {note} unexpectedly verified"
        tampered.append({"note": note, "canonical": blob.decode("utf-8"),
                         "verify": _rcpt(res)})

    b1_rec = next(r for r in receipts if r["kind"] == "note-b1"
                  and r["role"] == "recipient")

    def _flip_nullifier(d):
        nf = d["proof"]["nullifier"]
        d["proof"]["nullifier"] = nf[:-1] + ("0" if nf[-1] != "0" else "1")
    _tamper(b1_rec, _flip_nullifier, "b1 nullifier flipped")

    a1_rec = next(r for r in receipts if r["kind"] == "note-a1"
                  and r["role"] == "recipient")

    def _drop_sig(d):
        del d["proof"]["issuer_sig"]
    _tamper(a1_rec, _drop_sig, "a1 issuer_sig removed")

    def _wrong_value(d):
        d["txn"]["value"] = d["txn"]["value"] + 1
    _tamper(b1_rec, _wrong_value, "b1 txn value != face")

    out["tampered"] = tampered

    # ---- unilateral A2 flow -------------------------------------------------
    v_note = 777
    rho = draw()
    r_prime = draw()
    ua_nonces = [draw() for _ in range(7)]     # r_note, beta, gamma, k_r, k_b, k_s, k_g
    minted = mint_unilateral_a2(
        bob["sk"], bob["E"], alice["pk_recv"], v_note, rho, bob["addr"], CHAINID,
        r_prime=r_prime, rng=_replay(list(ua_nonces)))
    tree = IdentityTree()
    extra_M = mul(G1, draw())
    for P in (bob["M"], alice["M"], extra_M):
        tree.insert(P)
    t_vd = draw()
    rcpt = make_receipt(alice["k_recv"], alice["M"], minted, bob["addr"],
                        CHAINID, tree, rng=_replay([t_vd]))
    res = verify_ua2(rcpt, bob["pk"], bob["E"], tree.root(), tree)
    assert res.valid and res.reason == "VALID"
    empty_tree = IdentityTree()
    empty_tree.insert(alice["M"])
    res_bad = verify_ua2(rcpt, bob["pk"], bob["E"], empty_tree.root(), empty_tree)
    out["unilateral_a2"] = {
        "sk_iss": scalar_to_hex(bob["sk"]), "E_reg": _ct(bob["E"]),
        "M_rec": _g1(alice["M"]), "m_rec": scalar_to_hex(alice["m"]),
        "pk_recv": _g1(alice["pk_recv"]), "k_recv": scalar_to_hex(alice["k_recv"]),
        "v": _hx(v_note), "rho": scalar_to_hex(rho),
        "issuer": _hx(bob["addr"]), "chainid": _hx(CHAINID),
        "predicate": _hx(0),
        "r_prime": scalar_to_hex(r_prime),
        "r_note": scalar_to_hex(ua_nonces[0]),
        "beta": scalar_to_hex(ua_nonces[1]), "gamma": scalar_to_hex(ua_nonces[2]),
        "k_r": scalar_to_hex(ua_nonces[3]), "k_b": scalar_to_hex(ua_nonces[4]),
        "k_s": scalar_to_hex(ua_nonces[5]), "k_g": scalar_to_hex(ua_nonces[6]),
        "minted": {
            "eNote": _ct(minted.eNote), "eIss": _ct(minted.eIss),
            "M_I": _g1(minted.M_I), "idHash": _hx(minted.idHash),
            "cm": _hx(minted.cm), "opening": _opening_json(minted.opening),
            "binding": _binding_json(minted.binding),
            "gamma": scalar_to_hex(minted.gamma),
        },
        "tree": {"depth": tree.depth,
                 "leaves": [_hx(l) for l in tree.leaves],
                 "root": _hx(tree.root())},
        "t_vd": scalar_to_hex(t_vd),
        "receipt": {"M_I": _g1(rcpt.M_I), "M_rec": _g1(rcpt.M_rec),
                    "pk_recv": _g1(rcpt.pk_recv),
                    "value": _hx(rcpt.value), "vd": _vd_json(rcpt.vd),
                    "gamma": scalar_to_hex(rcpt.gamma),
                    "M_I_member": rcpt.M_I_member,
                    "M_rec_member": rcpt.M_rec_member},
        "verify": {"valid": res.valid, "reason": res.reason},
        "wrong_root_tree": {"leaves": [_hx(l) for l in empty_tree.leaves],
                            "root": _hx(empty_tree.root())},
        "wrong_root_verify": {"valid": res_bad.valid, "reason": res_bad.reason},
    }

    # ---- unilateral A1 flow -------------------------------------------------
    rho = draw()
    r_prime, r_note = draw(), draw()
    minted1 = mint_unilateral_a1(alice["M"], alice["pk_recv"], v_note, rho,
                                 bob["m"], r_prime=r_prime, rng=_replay([r_note]))
    t_vd = draw()
    rcpt1 = make_receipt_a1(alice["k_recv"], alice["M"], minted1, bob["M"],
                            bob["addr"], CHAINID, tree, rng=_replay([t_vd]))
    res1 = verify_receipt_a1(rcpt1, tree.root(), tree)
    assert res1.valid
    out["unilateral_a1"] = {
        "M_rec": _g1(alice["M"]), "m_rec": scalar_to_hex(alice["m"]),
        "pk_recv": _g1(alice["pk_recv"]), "k_recv": scalar_to_hex(alice["k_recv"]),
        "v": _hx(v_note), "rho": scalar_to_hex(rho),
        "m_issuer": scalar_to_hex(bob["m"]), "M_iss": _g1(bob["M"]),
        "issuer": _hx(bob["addr"]), "chainid": _hx(CHAINID),
        "predicate": _hx(0),
        "r_prime": scalar_to_hex(r_prime), "r_note": scalar_to_hex(r_note),
        "minted": {
            "eNote": _ct(minted1.eNote), "eRec": _ct(minted1.eRec),
            "idHash": _hx(minted1.idHash), "cm": _hx(minted1.cm),
            "opening": _opening_json(minted1.opening),
        },
        "t_vd": scalar_to_hex(t_vd),
        "receipt": {"M_iss": _g1(rcpt1.M_iss), "M_rec": _g1(rcpt1.M_rec),
                    "pk_recv": _g1(rcpt1.pk_recv),
                    "value": _hx(rcpt1.value), "vd": _vd_json(rcpt1.vd),
                    "M_iss_member": rcpt1.M_iss_member,
                    "M_rec_member": rcpt1.M_rec_member},
        "verify": {"valid": res1.valid, "reason": res1.reason},
    }

    # ---- issuer ceremony ----------------------------------------------------
    kx, ky = draw(), draw()
    issuer = Issuer(issuer_id="atb-financial-ca",
                    issuer_addr=0x1553E12000000000000000000000000000000001,
                    keypair=ps_keygen(rng=_replay([kx, ky])))
    t_sig, r_del = draw(), draw()
    cred = issuer.issue({"given_name": "Carol", "surname": "Mill",
                         "issuer_id": "IGNORED-overwritten"},
                        applicant_addr=0xCA401,
                        applicant_pk=alice["pk"],
                        rng=_replay([t_sig, r_del]))
    assert issuer.verify_credential(cred)
    from alberta_buck.wallet.bn254 import g2_to_words
    def _g2j(P):
        (x0, x1), (y0, y1) = g2_to_words(P)
        return {"x": [_hx(x0), _hx(x1)], "y": [_hx(y0), _hx(y1)]}
    out["issuer"] = {
        "sk_x": scalar_to_hex(kx), "sk_y": scalar_to_hex(ky),
        "pk_X": _g2j(issuer.pk_X), "pk_Y": _g2j(issuer.pk_Y),
        "pk_Y1": _g1(issuer.pk_Y1),
        "issuer_id": "atb-financial-ca",
        "fields": {"given_name": "Carol", "surname": "Mill",
                   "issuer_id": "IGNORED-overwritten"},
        "applicant_pk": _g1(alice["pk"]),
        "t_sig": scalar_to_hex(t_sig), "r_delivery": scalar_to_hex(r_del),
        "canonical": cred.canonical, "m": scalar_to_hex(cred.m),
        "sigma_1": _g1(cred.sigma.sigma_1), "sigma_2": _g1(cred.sigma.sigma_2),
        "delivery": _ct(cred.delivery),
    }

    # ---- Notes: the mailbox, the delivery, the binding, the folded gate ------
    #
    # One row shape, {fn, args, want}, so each backend replays the section with
    # one loop.  Arguments and results are hex; a delivery and a fold witness
    # are DOCUMENTS and keep their decimal words.  Drawn after every other row,
    # so adding them moves nothing above.
    out["notes"] = _notes_section(alice, bob, draw, rng)

    return out


def _notes_section(alice, bob, draw, rng) -> List[Dict[str, Any]]:
    from alberta_buck.wallet.delivery import deliver_a1, deliver_a2, open_a1, open_a2
    from alberta_buck.wallet.deposit_fold import (
        deposit_fold_witness, deposit_fold_a1_witness, deposit_fold_a2_witness,
    )
    from alberta_buck.wallet.recvkey import wrap_mask
    from alberta_buck.wallet.unilateral_a1 import mint_unilateral_a1
    from alberta_buck.wallet.unilateral_a2 import mint_unilateral_a2
    from alberta_buck.registry.merkle_service import rooted_registry
    from alberta_buck.registry.tree import (
        AGGREGATOR_DEPTH, KYC_SUBTREE_DEPTH, identity_leaf_salted, mailbox_leaf, receiving_leaf,
    )

    rows: List[Dict[str, Any]] = []

    def row(fn, args, want):
        rows.append({"fn": fn, "args": args, "want": want})

    def opening(o, cm) -> Dict[str, Any]:
        return {"flavor": o.flavor, "v": _hx(o.v), "rho": _hx(o.rho), "idHash": _hx(o.id_hash),
                "predicate": _hx(o.predicate), "cm": _hx(cm)}

    def path(proof) -> Dict[str, Any]:
        return {"siblings": [_hx(x) for x in proof.siblings], "indexBits": list(proof.index_bits)}

    k_a, pk_a = alice["k_recv"], alice["pk_recv"]
    face = 250

    # the receiving key, at two rotations of one seed
    for rot in (0, 1):
        k, pk = receiving_key(alice["wallet_seed"], rot)
        row("receiving_key", {"seed": _hx(alice["wallet_seed"]), "rotation": rot},
            {"k": scalar_to_hex(k), "pk_recv": _g1(pk)})

    # one field's mask
    shared = mul(G1, draw())
    row("wrap_mask", {"shared": _g1(shared), "label": "rho"},
        scalar_to_hex(wrap_mask(shared, b"rho")))

    # A1: mint, deliver, open
    rho1, rp1, rn1 = draw(), draw(), draw()
    m1 = mint_unilateral_a1(alice["M"], pk_a, v=face, rho=rho1, m_issuer=bob["m"],
                            r_prime=rp1, rng=_replay([rn1]))
    d1 = deliver_a1(m1, pk_a)
    row("deliver_a1", {"eNote": _ct(m1.eNote), "eRec": _ct(m1.eRec), "v": _hx(face),
                       "rho": scalar_to_hex(rho1), "predicate": _hx(0),
                       "r_note": scalar_to_hex(m1.r_note), "pk_recv": _g1(pk_a)}, d1)
    o1 = open_a1(d1, k_a, bob["m"])
    row("open_a1", {"delivery": d1, "k": scalar_to_hex(k_a), "m_issuer": scalar_to_hex(bob["m"])},
        {"opening": opening(o1.opening, o1.cm), "eNote": _ct(o1.eNote), "eRec": _ct(o1.eRec),
         "r_note": _hx(o1.r_note)})

    # A2: mint (with the issuer's naming salt), deliver, open
    rho2, salt_iss = draw(), draw() % F_R or 1
    m2 = mint_unilateral_a2(bob["sk"], bob["E"], pk_a, v=face, rho=rho2, issuer=bob["addr"],
                            chainid=CHAINID, salt_iss=salt_iss, rng=rng)
    d2 = deliver_a2(m2, pk_a)
    row("deliver_a2", {"eNote": _ct(m2.eNote), "eIss": _ct(m2.eIss), "T": _g1(m2.binding.T),
                       "v": _hx(face), "rho": scalar_to_hex(rho2), "predicate": _hx(0),
                       "r_note": scalar_to_hex(m2.r_note), "r_prime": scalar_to_hex(m2.r_prime),
                       "salt_iss": scalar_to_hex(salt_iss), "gamma": scalar_to_hex(m2.gamma),
                       "pk_recv": _g1(pk_a)}, d2)
    o2 = open_a2(d2, k_a)
    row("open_a2", {"delivery": d2, "k": scalar_to_hex(k_a)},
        {"opening": opening(o2.opening, o2.cm), "eNote": _ct(o2.eNote), "eIss": _ct(o2.eIss),
         "M_I": _g1(o2.M_I), "r_prime": _hx(o2.r_prime), "salt_iss": _hx(o2.salt_iss),
         "T": _g1(o2.T), "gamma": _hx(o2.gamma)})

    # One registry subtree holding the recipient's three associations' worth of
    # leaves -- the gate's (scalars), the payer's (points), and the A2 issuer's
    # naming leaf -- under an aggregator, so every path runs 32 levels.
    salt_rec, salt_mbx = draw() % F_R or 1, draw() % F_R or 1
    tree = rooted_registry()
    for leaf in (receiving_leaf(alice["m"], k_a, salt_rec),
                 mailbox_leaf(alice["M"], pk_a, salt_mbx),
                 identity_leaf_salted(bob["M"], salt_iss)):
        tree.insert_leaf(leaf)
    root = tree.root()
    aggregator = {"leaves": [_hx(x) for x in tree.service._tree.leaves], "depth": AGGREGATOR_DEPTH,
                  "slot": tree.service.get_sub_tree(tree.sub_tree_id).aggregator_leaf_index}

    # the mailbox binding: a payer's check, with no secret
    b = prove_receiving_binding(alice["M"], pk_a, salt_mbx, tree)
    binding = {"pk_recv": _g1(pk_a), "salt": _hx(salt_mbx),
               "path": {"leaf": _hx(b.path.leaf), **path(b.path), "root": _hx(b.path.root)}}
    row("prove_receiving_binding",
        {"M_rec": _g1(alice["M"]), "pk_recv": _g1(pk_a), "salt": _hx(salt_mbx),
         "leaves": [_hx(x) for x in tree.leaves], "depth": KYC_SUBTREE_DEPTH,
         "aggregator": aggregator}, binding)
    row("verify_receiving_binding", {"M_rec": _g1(alice["M"]), "binding": binding, "root": _hx(root)},
        True)

    # the folded gate: a fresh deposit account registered to the recipient
    sk_dep, r_E = draw(), draw()
    pk_dep = mul(G1, sk_dep)
    E_dep = elgamal_encrypt(alice["M"], pk_dep, r_E)
    rec_path = tree.path(0)

    def common(t, eEnc, o):
        return {"m_rec": scalar_to_hex(alice["m"]), "k": scalar_to_hex(k_a),
                "sk_dep": scalar_to_hex(sk_dep), "salt": _hx(salt_rec), "path": path(rec_path),
                "t": scalar_to_hex(t), "r_E": scalar_to_hex(r_E), "E_dep": _ct(E_dep),
                "pk_dep": _g1(pk_dep), "eEnc": _ct(eEnc), "rho": _hx(o.opening.rho),
                "idHash": _hx(o.opening.id_hash), "identityRoot": _hx(root)}

    t1 = draw()
    eEnc1 = elgamal_encrypt(alice["M"], pk_a, t1)
    w1 = deposit_fold_witness(m_rec=alice["m"], k=k_a, sk_dep=sk_dep, salt=salt_rec, E_dep=E_dep,
                              note_ct=eEnc1, tree=tree)
    row("deposit_fold_a1_witness",
        {**common(t1, eEnc1, o1), "eNote": _ct(o1.eNote), "v": _hx(face),
         "m_issuer": scalar_to_hex(bob["m"]), "r_note": scalar_to_hex(o1.r_note)},
        deposit_fold_a1_witness(witness=w1, rho=o1.opening.rho, id_hash=o1.opening.id_hash,
                                e_note=o1.eNote, v=face, m_issuer=bob["m"],
                                r_note=o1.r_note, t=t1, r_E=r_E, e_dep=E_dep,
                                pk_dep=pk_dep, e_enc=eEnc1, identity_root=root))

    t2 = draw()
    eEnc2 = elgamal_encrypt(o2.M_I, pk_a, t2)
    w2 = deposit_fold_witness(m_rec=alice["m"], k=k_a, sk_dep=sk_dep, salt=salt_rec, E_dep=E_dep,
                              note_ct=eEnc2, tree=tree)
    iss_path = tree.path(2)
    row("deposit_fold_a2_witness",
        {**common(t2, eEnc2, o2), "eNote": _ct(o2.eNote), "eIss": _ct(o2.eIss),
         "r_prime": scalar_to_hex(o2.r_prime), "salt_iss": _hx(o2.salt_iss),
         "issPath": path(iss_path), "T": _g1(o2.T), "gamma": scalar_to_hex(o2.gamma)},
        deposit_fold_a2_witness(witness=w2, rho=o2.opening.rho, id_hash=o2.opening.id_hash,
                                e_note=o2.eNote, e_iss=o2.eIss, r_prime=o2.r_prime, t=t2,
                                r_E=r_E, e_dep=E_dep, pk_dep=pk_dep, e_enc=eEnc2,
                                salt_iss=o2.salt_iss, iss_path=iss_path, T=o2.T, gamma=o2.gamma,
                                identity_root=root))
    return rows


def emit_wallet_vectors(path: str, seed: int = 0x3A11E75EED) -> Dict[str, Any]:
    data = build_wallet_vectors(seed=seed)
    with open(path, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    return data


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "core/vectors/wallet-kernel-vectors.json"
    data = emit_wallet_vectors(out)
    sys.stderr.write(f"wrote {out} ({len(json.dumps(data))} bytes JSON)\n")
