"""Wallet-kernel conformance, Python side.

Replays core/vectors/wallet-kernel-vectors.json (emitted by the Python
reference via alberta_buck.wallet.wallet_kernel_vectors, nonces
included) through the buck_core.buck_wallet binding: the canonical JSON
dialect, the AB-RCPT/2 envelope, every receipt build (JSON-args ABI),
the tier-1 verifier, the unilateral A1/A2 flows and the issuer
ceremony.  The Rust and JS suites assert the same file.

Build the kernel binding first: make nix-core-build-py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

bw = pytest.importorskip(
    "buck_core.buck_wallet",
    reason="kernel binding not built (make nix-core-build-py)",
)

_REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def wv() -> dict:
    return json.loads((_REPO / "core/vectors/wallet-kernel-vectors.json").read_text(encoding="utf-8"))


def test_canonical_json(wv):
    for row in wv["canonical_json"]:
        assert bw.canonical_json(row["input"]) == row["canonical"]
    with pytest.raises(ValueError):
        bw.canonical_json('{"x": 1.5}')


def test_envelope(wv):
    for row in wv["envelope"]:
        blob = row["canonical"].encode("utf-8")
        assert bw.receipt_id(blob) == row["id12"]
        assert bw.receipt_id(blob, 20) == row["id20"]
        assert bw.envelope_text(blob) == row["envelope64"]
        assert bw.envelope_text(blob, 8) == row["envelope8"]
        assert bw.parse_envelope(row["envelope64"]) == blob
        assert bw.parse_envelope(row["noisy"]) == blob
    with pytest.raises(ValueError):
        bw.parse_envelope("no header .END")


def _party_args(wv, name):
    p = wv["parties"][name]
    return {"addr": p["addr"], "identity": p["identity"], "M": p["M"],
            "pk": p["pk"], "E": p["E"], "sk": p["sk"]}


def _receipt_args(wv, row):
    kind = row["kind"]
    args = {
        "kind": "note-a2" if kind == "note-a2-unbound" else kind,
        "role": row["role"],
        "chainid": wv["chainid"],
        "contracts": wv["contracts"],
        "payer": _party_args(wv, row["payer"]),
        "payee": _party_args(wv, row["payee"]),
        "txn": row["txn"],
        "nonces": row["nonces"],
    }
    if kind == "eoa-priv":
        args["E_for_payee"] = row["E_for_payee"]
        args["cp_proof"] = row["cp_proof"]
    if kind.startswith("note-"):
        mint = row["mint"]
        args["opening"] = mint["opening"]
        args["cms"] = mint["cms"]
        args["nullifier"] = mint["nullifier"]
        args["face"] = mint["opening"]["v"]
        if kind in ("note-b1", "note-a1"):
            args["issuer_sig"] = mint["issuer_sig"]
        if kind == "note-b1":
            args["eDepForIss"] = mint["eDepForIss"]
        if kind == "note-a1":
            args["eNote"] = mint["eNote"]
            args["eRec"] = mint["eRec"]
        if kind.startswith("note-a2"):
            args["eNote"] = mint["eNote"]
            args["eIss"] = mint["eIss"]
            # idHash commits the binding's T either way; gamma opens it.
            args["T"] = mint["binding"]["T"]
            args["gamma"] = mint["nonces"]["gamma"]
            if kind == "note-a2":
                args["binding"] = mint["binding"]
        if kind.startswith("note-a1") or kind.startswith("note-a2"):
            # The addressed legs: the mailbox key, and whichever evidence the
            # generating role could produce.  The recipient holds k; the issuer
            # holds the randomness it encrypted with.
            alice = wv["parties"][row["payee"]]
            args["pk_recv"] = alice["pk_recv"]
            args["mailbox_binding"] = row.get("mailboxBinding")
            if row["role"] == "recipient":
                args["k_recv"] = alice["k_recv"]
            else:
                args["r_note"] = mint["nonces"]["r_note"]
                args["r_id"] = mint["nonces"][
                    "r_rec" if kind == "note-a1" else "r_prime"]
    return args


def _check_verify(got_json: str, want: dict):
    got = json.loads(got_json)
    assert got["ok"] == want["ok"]
    assert got["reason"] == want["reason"]
    if want.get("identity_M"):
        assert int(got["identity_M"]["x"], 16) == int(want["identity_M"]["x"], 16)
        assert int(got["identity_M"]["y"], 16) == int(want["identity_M"]["y"], 16)
    if want.get("value") is not None:
        assert int(got["value"], 16) == want["value"]


def test_receipts_build_and_verify(wv):
    for row in wv["receipts"]:
        canonical = bw.build_receipt(json.dumps(_receipt_args(wv, row)))
        assert canonical == row["canonical"], f'{row["kind"]} {row["role"]}'
        blob = canonical.encode("utf-8")
        assert bw.receipt_id(blob) == row["receipt_id"]
        assert bw.envelope_text(blob) == row["envelope"]
        _check_verify(bw.verify_receipt(canonical), row["verify"])


def test_tampered(wv):
    for row in wv["tampered"]:
        got = json.loads(bw.verify_receipt(row["canonical"]))
        assert not got["ok"], row["note"]
        assert got["reason"] == row["verify"]["reason"], row["note"]


def test_unilateral_a2(wv):
    u = wv["unilateral_a2"]
    mint_args = {
        "sk_iss": u["sk_iss"], "E_reg": u["E_reg"], "pk_recv": u["pk_recv"],
        "v": u["v"], "rho": u["rho"], "issuer": u["issuer"],
        "chainid": u["chainid"], "predicate": u["predicate"],
        "nonces": {k: u[k] for k in
                   ("r_prime", "r_note", "beta", "gamma",
                    "k_r", "k_b", "k_s", "k_g")},
    }
    minted = json.loads(bw.mint_unilateral_a2(json.dumps(mint_args)))
    for key in ("eNote", "eIss", "M_I", "idHash", "cm", "opening", "binding"):
        assert minted[key] == u["minted"][key], key

    rcpt = json.loads(bw.make_receipt_a2(json.dumps({
        "k_recv": u["k_recv"], "M_rec": u["M_rec"],
        "minted": minted, "issuer": u["issuer"],
        "chainid": u["chainid"], "tree": {"depth": u["tree"]["depth"],
                                          "leaves": u["tree"]["leaves"]},
        "t_vd": u["t_vd"],
    })))
    assert rcpt["M_I"] == u["receipt"]["M_I"]
    assert rcpt["M_rec"] == u["receipt"]["M_rec"]
    assert rcpt["pk_recv"] == u["receipt"]["pk_recv"]
    assert rcpt["vd"] == u["receipt"]["vd"]
    assert rcpt["M_I_member"] == u["receipt"]["M_I_member"]
    assert rcpt["M_rec_member"] == u["receipt"]["M_rec_member"]

    res = json.loads(bw.verify_receipt_a2(json.dumps({
        "receipt": rcpt, "pk_iss": wv["parties"]["bob"]["pk"],
        "E_reg": u["E_reg"], "identity_root": u["tree"]["root"],
        "tree": {"depth": u["tree"]["depth"], "leaves": u["tree"]["leaves"]},
    })))
    assert res["valid"] == u["verify"]["valid"]
    assert res["reason"] == u["verify"]["reason"]

    res_bad = json.loads(bw.verify_receipt_a2(json.dumps({
        "receipt": rcpt, "pk_iss": wv["parties"]["bob"]["pk"],
        "E_reg": u["E_reg"], "identity_root": u["wrong_root_tree"]["root"],
        "tree": {"depth": 10, "leaves": u["wrong_root_tree"]["leaves"]},
    })))
    assert res_bad["valid"] == u["wrong_root_verify"]["valid"]
    assert res_bad["reason"] == u["wrong_root_verify"]["reason"]


def test_unilateral_a1(wv):
    u = wv["unilateral_a1"]
    tree = {"depth": wv["unilateral_a2"]["tree"]["depth"],
            "leaves": wv["unilateral_a2"]["tree"]["leaves"]}
    minted = json.loads(bw.mint_unilateral_a1(json.dumps({
        "M_rec": u["M_rec"], "pk_recv": u["pk_recv"],
        "v": u["v"], "rho": u["rho"],
        "m_issuer": u["m_issuer"], "predicate": u["predicate"],
        "nonces": {"r_prime": u["r_prime"], "r_note": u["r_note"]},
    })))
    for key in ("eNote", "eRec", "idHash", "cm", "opening"):
        assert minted[key] == u["minted"][key], key

    rcpt = json.loads(bw.make_receipt_a1(json.dumps({
        "k_recv": u["k_recv"], "M_rec": u["M_rec"],
        "minted": minted, "M_iss": u["M_iss"],
        "issuer": u["issuer"], "chainid": u["chainid"], "tree": tree,
        "t_vd": u["t_vd"],
    })))
    assert rcpt["M_iss"] == u["receipt"]["M_iss"]
    assert rcpt["M_rec"] == u["receipt"]["M_rec"]
    assert rcpt["pk_recv"] == u["receipt"]["pk_recv"]
    assert rcpt["vd"] == u["receipt"]["vd"]

    res = json.loads(bw.verify_receipt_a1(json.dumps({
        "receipt": rcpt,
        "identity_root": wv["unilateral_a2"]["tree"]["root"],
        "tree": tree,
    })))
    assert res["valid"] == u["verify"]["valid"]
    assert res["reason"] == u["verify"]["reason"]


def test_issuer(wv):
    i = wv["issuer"]
    cred = json.loads(bw.issue_credential(json.dumps({
        "sk_x": i["sk_x"], "sk_y": i["sk_y"], "issuer_id": i["issuer_id"],
        "fields": i["fields"], "t_sig": i["t_sig"],
        "applicant_pk": i["applicant_pk"], "r_delivery": i["r_delivery"],
    })))
    assert cred["canonical"] == i["canonical"]
    assert int(cred["m"], 16) == int(i["m"], 16)
    assert cred["sigma_1"] == i["sigma_1"]
    assert cred["sigma_2"] == i["sigma_2"]
    assert cred["delivery"] == i["delivery"]


def test_notes_kernel_replay(wv):
    """The Notes section -- receiving key, delivery, mailbox binding, fold witnesses -- through the
    kernel's JSON-args entry points, compared as parsed JSON with the Python reference."""
    rows = wv["notes"]
    assert len(rows) >= 11, "the notes section lost rows"
    for row in rows:
        got = json.loads(getattr(bw, row["fn"])(json.dumps(row["args"])))
        assert got == row["want"], f"{row['fn']} diverges from the Python reference"
